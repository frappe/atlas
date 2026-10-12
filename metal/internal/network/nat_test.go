package network

import (
	"context"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func TestRuleCommentMatchesExactly(t *testing.T) {
	arguments := []string{"-A", "PREROUTING", "--comment", "metal-public-ipv4-vm-10"}

	if !hasRuleComment(arguments, "metal-public-ipv4-vm-10") {
		t.Fatal("exact comment did not match")
	}
	if hasRuleComment(arguments, "metal-public-ipv4-vm-1") {
		t.Fatal("comment prefix matched another VM")
	}
}

func TestPublicIPv4RuleCheckRemovesInsertPosition(t *testing.T) {
	step := []string{"iptables", "-t", "nat", "-I", "POSTROUTING", "1", "-s", "10.0.0.2", "-j", "SNAT"}
	want := []string{"iptables", "-t", "nat", "-C", "POSTROUTING", "-s", "10.0.0.2", "-j", "SNAT"}

	if check := ruleCheck(step); !slices.Equal(check, want) {
		t.Fatalf("rule check = %v, want %v", check, want)
	}
}

// The SNAT rule tests the original destination. A connection to the public
// address of another VM has a mesh destination after DNAT, and it must still
// leave with the public source, so its reply returns through the host.
func TestPublicIPv6StepsTranslateConnectionsToPublicAddresses(t *testing.T) {
	steps := publicIPv6Steps("vm-10", "2001:db8:5::7", "fdaa:1:0:2::a")

	want := []string{
		"ip6tables", "-t", "nat", "-I", "POSTROUTING", "1", "-s", "fdaa:1:0:2::a",
		"-m", "conntrack", "!", "--ctorigdst", "fdaa::/16",
		"-m", "comment", "--comment", "metal-public-ipv6-vm-10", "-j", "SNAT", "--to-source", "2001:db8:5::7",
	}
	if !slices.ContainsFunc(steps, func(step []string) bool { return slices.Equal(step, want) }) {
		t.Fatalf("steps = %v, want one matching %v", steps, want)
	}
}

func TestPublicIPv4StepsMapHostOriginatedTraffic(t *testing.T) {
	steps := publicIPv4Steps("vm-10", "metal-vm-10", "vpeer0", "10.6.26.130", "203.0.113.7")

	want := []string{
		"iptables", "-t", "nat", "-A", "OUTPUT", "-d", "203.0.113.7",
		"-m", "comment", "--comment", "metal-public-ipv4-vm-10",
		"-j", "DNAT", "--to-destination", "10.6.26.130",
	}
	if !slices.ContainsFunc(steps, func(step []string) bool { return slices.Equal(step, want) }) {
		t.Fatalf("steps = %v, want one matching %v", steps, want)
	}
}

func TestMaximumSegmentSizeRuleClampsBothDirections(t *testing.T) {
	rule := maximumSegmentSizeRule("vg-100000")

	want := []string{
		"FORWARD", "-o", "vg-100000",
		"-p", "tcp", "--tcp-flags", "SYN,RST", "SYN",
		"-j", "TCPMSS", "--clamp-mss-to-pmtu",
	}
	if !slices.Equal(rule, want) {
		t.Fatalf("rule = %v, want %v", rule, want)
	}
}

func TestDefaultRouteInterfaceFromFile(t *testing.T) {
	table := `Iface	Destination	Gateway 	Flags	RefCnt	Use	Metric	Mask		MTU	Window	IRTT
eth0	00000000	0102030A	0003	0	0	100	00000000	0	0	0
`
	path := filepath.Join(t.TempDir(), "route")
	if err := os.WriteFile(path, []byte(table), 0o644); err != nil {
		t.Fatal(err)
	}

	iface, err := defaultRouteInterfaceFromFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if iface != "eth0" {
		t.Fatalf("interface = %q, want %q", iface, "eth0")
	}

	empty := filepath.Join(t.TempDir(), "empty")
	if err := os.WriteFile(empty, []byte("Iface\tDestination\nlo\t0000007F\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := defaultRouteInterfaceFromFile(empty); err == nil {
		t.Fatal("missing default route did not fail")
	}
	if _, err := defaultRouteInterfaceFromFile(filepath.Join(t.TempDir(), "absent")); err == nil {
		t.Fatal("unreadable route table did not fail")
	}
}

func fakeIP(t *testing.T, fixtures map[string]string, commands ...string) string {
	t.Helper()

	if len(commands) == 0 {
		commands = []string{"ip"}
	}
	directory := t.TempDir()
	fixtureDir := filepath.Join(directory, "fixtures")
	if err := os.Mkdir(fixtureDir, 0o755); err != nil {
		t.Fatal(err)
	}
	for argv, output := range fixtures {
		if err := os.WriteFile(filepath.Join(fixtureDir, argv), []byte(output), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	logPath := filepath.Join(directory, "calls.log")
	script := "#!/bin/sh\n" +
		"echo \"$@\" >> \"" + logPath + "\"\n" +
		"key=$(printf '%s' \"$*\" | tr -c '[:alnum:]' '_')\n" +
		"if [ -f \"" + fixtureDir + "/$key\" ]; then cat \"" + fixtureDir + "/$key\"; fi\n" +
		"exit 0\n"
	for _, command := range commands {
		if err := os.WriteFile(filepath.Join(directory, command), []byte(script), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", directory+string(os.PathListSeparator)+os.Getenv("PATH"))
	return logPath
}

func fixtureKey(arguments ...string) string {
	return strings.Map(func(r rune) rune {
		if r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' {
			return r
		}
		return '_'
	}, strings.Join(arguments, " "))
}

func recordedCalls(t *testing.T, logPath string) string {
	t.Helper()

	calls, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	return string(calls)
}

func TestReleasePublicIPv4ClaimsDropsStaleInterfaces(t *testing.T) {
	fixtures := map[string]string{
		fixtureKey("-o", "link", "show"):                                    "1: lo: <LOOPBACK> mtu 65536\n7: vh-7@if9: <BROADCAST> mtu 1500\n",
		fixtureKey("-4", "route", "show", "dev", "vh-7", "proto", "static"): "192.0.2.10 dev vh-7 scope link\n192.0.2.11 dev vh-7 scope link\n",
		fixtureKey("-4", "neigh", "show", "proxy", "192.0.2.10"):            "192.0.2.10 dev ethB proxy\n192.0.2.10 dev ethA proxy\n",
		fixtureKey("-4", "neigh", "show", "proxy", "192.0.2.11"):            "192.0.2.11 dev ethA proxy\n",
	}
	logPath := fakeIP(t, fixtures)

	if err := releasePublicIPv4Claims(context.Background(), 7, "192.0.2.10", "ethB"); err != nil {
		t.Fatal(err)
	}
	calls := recordedCalls(t, logPath)
	if !strings.Contains(calls, "-4 neigh del proxy 192.0.2.11 dev ethA") {
		t.Errorf("stale proxy entry was not deleted:\n%s", calls)
	}
	if !strings.Contains(calls, "-4 neigh del proxy 192.0.2.10 dev ethA") {
		t.Errorf("moved wanted entry was not deleted from the old interface:\n%s", calls)
	}
	if strings.Contains(calls, "del proxy 192.0.2.10 dev ethB") {
		t.Errorf("kept proxy entry was deleted:\n%s", calls)
	}
	if !strings.Contains(calls, "route del 192.0.2.11/32 dev vh-7") {
		t.Errorf("stale host route was not deleted:\n%s", calls)
	}
	if strings.Contains(calls, "route del 192.0.2.10") {
		t.Errorf("kept host route was deleted:\n%s", calls)
	}

	if err := os.WriteFile(logPath, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := releasePublicIPv4Claims(context.Background(), 7, "", ""); err != nil {
		t.Fatal(err)
	}
	calls = recordedCalls(t, logPath)
	if !strings.Contains(calls, "-4 neigh del proxy 192.0.2.10 dev ethB") {
		t.Errorf("full release did not delete every entry:\n%s", calls)
	}
}

func TestReleasePublicIPv6ClaimsDropsStaleInterfaces(t *testing.T) {
	fixtures := map[string]string{
		fixtureKey("-o", "link", "show"): "1: lo: <LOOPBACK> mtu 65536\n7: vh-7@if9: <BROADCAST> mtu 1500\n",
		fixtureKey("-6", "route", "show", "dev", "vh-7"): "2001:db8::10 via fe80::1 dev vh-7 metric 1024\n" +
			"2001:db8::11 via fe80::1 dev vh-7 metric 1024\n",
		fixtureKey("-6", "neigh", "show", "proxy", "2001:db8::10"): "2001:db8::10 dev ethB proxy\n2001:db8::10 dev ethA proxy\n",
		fixtureKey("-6", "neigh", "show", "proxy", "2001:db8::11"): "2001:db8::11 dev ethA proxy\n",
	}
	logPath := fakeIP(t, fixtures)

	if err := releasePublicIPv6Claims(context.Background(), 7, "2001:db8::10/128", "ethB"); err != nil {
		t.Fatal(err)
	}
	calls := recordedCalls(t, logPath)
	if !strings.Contains(calls, "-6 neigh del proxy 2001:db8::11 dev ethA") {
		t.Errorf("stale proxy entry was not deleted:\n%s", calls)
	}
	if !strings.Contains(calls, "-6 neigh del proxy 2001:db8::10 dev ethA") {
		t.Errorf("moved wanted entry was not deleted from the old interface:\n%s", calls)
	}
	if strings.Contains(calls, "del proxy 2001:db8::10 dev ethB") {
		t.Errorf("kept proxy entry was deleted:\n%s", calls)
	}
}

func TestReleaseClaimsWithoutVethSucceedsEmpty(t *testing.T) {
	fixtures := map[string]string{
		fixtureKey("-o", "link", "show"): "1: lo: <LOOPBACK> mtu 65536\n",
	}
	logPath := fakeIP(t, fixtures)

	if err := releasePublicIPv4Claims(context.Background(), 9, "", ""); err != nil {
		t.Fatal(err)
	}
	if err := releasePublicIPv6Claims(context.Background(), 9, "", ""); err != nil {
		t.Fatal(err)
	}
	if calls := recordedCalls(t, logPath); strings.Contains(calls, " del ") {
		t.Errorf("release without veth deleted state:\n%s", calls)
	}
}
