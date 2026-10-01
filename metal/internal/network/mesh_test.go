package network

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func stubMeshCommand(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "atlas-wg-mesh")
	if err := os.WriteFile(path, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestNewMeshNeedsACommandAndBothInterfaces(t *testing.T) {
	complete := MeshConfig{
		CommandPath: stubMeshCommand(t), WireGuardName: "wg0", UplinkName: "eno1.1878",
		WireGuardStatePath: "/var/lib/metal/wireguard-peers.json",
	}
	for name, incomplete := range map[string]MeshConfig{
		"no command path": {WireGuardName: complete.WireGuardName, UplinkName: complete.UplinkName, WireGuardStatePath: complete.WireGuardStatePath},
		"no WireGuard":    {CommandPath: complete.CommandPath, UplinkName: complete.UplinkName, WireGuardStatePath: complete.WireGuardStatePath},
		"no uplink":       {CommandPath: complete.CommandPath, WireGuardName: complete.WireGuardName, WireGuardStatePath: complete.WireGuardStatePath},
		"no peer state":   {CommandPath: complete.CommandPath, WireGuardName: complete.WireGuardName, UplinkName: complete.UplinkName},
	} {
		if _, err := NewMesh(incomplete); err == nil {
			t.Errorf("accepted a configuration with %s", name)
		}
	}
	if _, err := NewMesh(complete); err != nil {
		t.Errorf("NewMesh = %v", err)
	}
}

func TestMeshNamespaceStepsRouteTheGuestAddress(t *testing.T) {
	steps := meshNamespaceSteps("vg-100", "fdaa:1:0:7::1")
	lines := make([]string, len(steps))
	for index, step := range steps {
		lines[index] = strings.Join(step, " ")
	}
	joined := strings.Join(lines, "\n")
	for _, wanted := range []string{
		"sysctl -q -w net.ipv6.conf.all.forwarding=1",
		"sysctl -q -w net.ipv6.conf.vg-100.proxy_ndp=1",
		"sysctl -q -w net.ipv6.neigh.vg-100.proxy_delay=0",
		"link set vg-100 mtu 1380",
		"addr replace fe80::1/64 dev tap0 nodad",
		"route replace fdaa:1:0:7::1/128 dev tap0",
		"neigh replace fdaa:1:0:7::1 lladdr 06:00:ac:10:00:02 dev tap0 nud permanent",
		"route replace fdaa::/16 via fe80::1 dev vg-100",
		"neigh replace proxy fdaa:1:0:7::1 dev vg-100",
	} {
		if !strings.Contains(joined, wanted) {
			t.Errorf("missing step %q in:\n%s", wanted, joined)
		}
	}
}

func TestGatewayRouteStepsSendHostTrafficToTheGuest(t *testing.T) {
	route := "ip -6 route replace default via fdaa:1::5 dev tap0 table 100"
	rule := "ip -6 rule add iif vg-100 lookup 100"
	for _, test := range []struct {
		name               string
		isGateway, hasRule bool
		want               []string
	}{
		{"add", true, false, []string{route, rule}},
		{"keep", true, true, []string{route}},
		{"remove", false, true, []string{"ip -6 rule del iif vg-100 lookup 100", "ip -6 route flush table 100"}},
		{"absent", false, false, nil},
	} {
		var got []string
		for _, step := range gatewayRouteSteps("vg-100", "fdaa:1::5", test.isGateway, test.hasRule) {
			got = append(got, strings.Join(step, " "))
		}
		if !slices.Equal(got, test.want) {
			t.Errorf("%s: got %q, want %q", test.name, got, test.want)
		}
	}
}

func TestNewMeshNeedsTheCommandOnTheHost(t *testing.T) {
	if _, err := NewMesh(MeshConfig{
		CommandPath: "/nonexistent/atlas-wg-mesh", WireGuardName: "wg0", UplinkName: "eno1.1878",
		WireGuardStatePath: "/var/lib/metal/wireguard-peers.json",
	}); err == nil {
		t.Error("accepted a command path that is not on the host")
	}
}

func recordingMesh(t *testing.T) (*Mesh, string) {
	t.Helper()
	directory := t.TempDir()
	callLog := filepath.Join(directory, "calls")
	command := filepath.Join(directory, "atlas-wg-mesh")
	script := fmt.Sprintf("#!/bin/sh\necho \"$@\" >> %s\n", callLog)
	if err := os.WriteFile(command, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	mesh, err := NewMesh(MeshConfig{
		CommandPath: command, WireGuardName: "wg0", UplinkName: "eno1", ControllerAddress: "fdaa:1::ffff:ffff:ffff:ffff",
		WireGuardStatePath: "/var/lib/metal/wireguard-peers.json",
	})
	if err != nil {
		t.Fatal(err)
	}
	return mesh, callLog
}

func readCalls(t *testing.T, path string) []string {
	t.Helper()
	contents, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return strings.Split(strings.TrimSpace(string(contents)), "\n")
}

func TestMeshUsesConvergentHostCommands(t *testing.T) {
	mesh, callsPath := recordingMesh(t)
	ctx := context.Background()
	if err := mesh.EnsureHost(ctx); err != nil {
		t.Fatal(err)
	}
	if err := mesh.SyncPeerState(ctx, true); err != nil {
		t.Fatal(err)
	}
	if err := mesh.ApplyPrivilegedAddresses(ctx, []string{"fdaa:1::1", "fdaa:1::2"}); err != nil {
		t.Fatal(err)
	}
	if err := mesh.ApplyPrivilegedAddresses(ctx, nil); err != nil {
		t.Fatal(err)
	}

	want := []string{
		"configure --uplink eno1 --wireguard wg0 --controller fdaa:1::ffff:ffff:ffff:ffff",
		"peers sync /var/lib/metal/wireguard-peers.json --unicast",
		"privileged-vm replace fdaa:1::1 fdaa:1::2",
		"privileged-vm clear",
	}
	got := readCalls(t, callsPath)
	if strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("calls:\n%s\nwant:\n%s", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
}

func TestMeshSelectsMulticastWithoutTheFlag(t *testing.T) {
	mesh, callsPath := recordingMesh(t)
	if err := mesh.SyncPeerState(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	if got := readCalls(t, callsPath)[0]; got != "peers sync /var/lib/metal/wireguard-peers.json" {
		t.Fatalf("call = %q", got)
	}
}
