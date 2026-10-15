package network

import (
	"bytes"
	"context"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeHostCommands puts ip and sysctl stubs first on PATH. ip reports an existing proxy entry
// when the proxy file exists, and every command is logged.
func fakeHostCommands(t *testing.T, isClaimed bool) (commandLog string) {
	t.Helper()
	directory := t.TempDir()
	commandLog = filepath.Join(directory, "commands.log")
	proxyFile := filepath.Join(directory, "proxy")
	if isClaimed {
		if err := os.WriteFile(proxyFile, []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	script := "#!/bin/sh\necho \"$0 $*\" >> " + commandLog + "\n" +
		"case \"$*\" in *\"neigh show proxy\"*) [ -e " + proxyFile + " ] && echo proxy;; esac\nexit 0\n"
	for _, name := range []string{"ip", "sysctl"} {
		if err := os.WriteFile(filepath.Join(directory, name), []byte(script), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", directory+string(os.PathListSeparator)+os.Getenv("PATH"))
	return commandLog
}

func readCommands(t *testing.T, commandLog string) string {
	t.Helper()
	data, err := os.ReadFile(commandLog)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestClaimPublicIPv4AnnouncesANewClaim(t *testing.T) {
	commandLog := fakeHostCommands(t, false)
	var announced []string
	announce := func(interfaceName, address string) error {
		announced = append(announced, interfaceName+" "+address)
		return nil
	}

	if err := claimPublicIPv4(context.Background(), 1, "bond0", "203.0.113.5", announce); err != nil {
		t.Fatal(err)
	}

	if len(announced) != 1 || announced[0] != "bond0 203.0.113.5" {
		t.Fatalf("announced = %v, want one announcement", announced)
	}
	if !strings.Contains(readCommands(t, commandLog), "neigh replace proxy 203.0.113.5 dev bond0") {
		t.Fatal("the proxy entry was not installed")
	}
}

func TestClaimPublicIPv4DoesNotAnnounceAnExistingClaim(t *testing.T) {
	commandLog := fakeHostCommands(t, true)
	announce := func(string, string) error {
		t.Fatal("an existing claim must not announce again")
		return nil
	}

	if err := claimPublicIPv4(context.Background(), 1, "bond0", "203.0.113.5", announce); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(readCommands(t, commandLog), "neigh replace proxy") {
		t.Fatal("the claim was not refreshed")
	}
}

func TestFailedAnnouncementLeavesNoProxyEntryForTheRetry(t *testing.T) {
	commandLog := fakeHostCommands(t, false)
	announce := func(string, string) error { return errors.New("no route to the interface") }

	if err := claimPublicIPv4(context.Background(), 1, "bond0", "203.0.113.5", announce); err == nil {
		t.Fatal("want the announcement error")
	}
	if strings.Contains(readCommands(t, commandLog), "neigh replace") {
		t.Fatal("a failed announcement installed the proxy entry, so the retry would skip it")
	}
}

func TestClaimPublicIPv6AnnouncesTheAddressWithoutItsPrefixLength(t *testing.T) {
	fakeHostCommands(t, false)
	var announced string
	announce := func(_, address string) error {
		announced = address
		return nil
	}

	if err := claimPublicIPv6(context.Background(), "bond0", "2001:db8::5/128", announce); err != nil {
		t.Fatal(err)
	}
	if announced != "2001:db8::5" {
		t.Fatalf("announced = %q", announced)
	}
}

func TestGratuitousARPFrameAnnouncesTheAddressToEveryHost(t *testing.T) {
	mac := net.HardwareAddr{0xc6, 0x39, 0x48, 0xe6, 0x5f, 0x38}
	address := [4]byte{103, 48, 90, 252}

	want := []byte{
		0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xc6, 0x39, 0x48, 0xe6, 0x5f, 0x38, 0x08, 0x06,
		0, 1, 0x08, 0, 6, 4, 0, 2,
		0xc6, 0x39, 0x48, 0xe6, 0x5f, 0x38, 103, 48, 90, 252,
		0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 103, 48, 90, 252,
	}
	if got := gratuitousARPFrame(mac, address); !bytes.Equal(got, want) {
		t.Fatalf("frame = % x\nwant   % x", got, want)
	}
}

func TestNeighborAdvertisementOverridesTheCacheWithTheHostMAC(t *testing.T) {
	mac := net.HardwareAddr{0xc6, 0x39, 0x48, 0xe6, 0x5f, 0x38}
	address := [16]byte{0x20, 0x01, 0x0d, 0xb8, 15: 1}

	message := neighborAdvertisement(mac, address)

	if message[0] != 136 || message[4]&0x20 == 0 {
		t.Fatalf("message = % x, want a neighbor advertisement with the override flag", message[:8])
	}
	if !bytes.Equal(message[8:24], address[:]) || !bytes.Equal(message[26:], mac) {
		t.Fatalf("message = % x, want the target address and the MAC option", message)
	}
}
