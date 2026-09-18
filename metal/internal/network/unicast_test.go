package network

import (
	"context"
	"errors"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type fakeUnicastCommands struct {
	runCommands   [][]string
	outputResults map[string]string
	outputErrors  map[string]error
}

func (commands *fakeUnicastCommands) Run(_ context.Context, name string, arguments ...string) error {
	commands.runCommands = append(commands.runCommands, append([]string{name}, arguments...))
	return nil
}

func (commands *fakeUnicastCommands) Output(_ context.Context, name string, arguments ...string) (string, error) {
	command := strings.Join(append([]string{name}, arguments...), " ")
	if err, found := commands.outputErrors[command]; found {
		return "", err
	}
	return commands.outputResults[command], nil
}

const uplinkAddressCommand = "ip -4 -o addr show dev eno1.1878 scope global"

func newTestUnicastManager(t *testing.T) (*UnicastManager, *fakeUnicastCommands) {
	t.Helper()

	directory := t.TempDir()
	configuration := UnicastConfig{
		PeersFilePath: filepath.Join(directory, "unicast-peers"),
		UplinkName:    "eno1.1878",
		UnitFilePath:  filepath.Join(directory, UnicastUnitName),
	}
	if err := os.WriteFile(configuration.UnitFilePath, []byte("[Unit]\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	manager, err := NewUnicastManager(configuration)
	if err != nil {
		t.Fatal(err)
	}
	commands := &fakeUnicastCommands{
		outputResults: map[string]string{
			uplinkAddressCommand: "2: eno1.1878 inet 10.20.0.11/24 scope global eno1.1878\n",
		},
		outputErrors: make(map[string]error),
	}
	manager.commands = commands
	return manager, commands
}

func unicastCommandText(commands [][]string) []string {
	texts := make([]string, len(commands))
	for index, command := range commands {
		texts[index] = strings.Join(command, " ")
	}
	return texts
}

func mustUnicastAddress(t *testing.T, value string) netip.Addr {
	t.Helper()

	address, err := netip.ParseAddr(value)
	if err != nil {
		t.Fatal(err)
	}
	return address
}

func TestUnicastManagerApplyWritesThePeersFileAndStartsTheDaemon(t *testing.T) {
	manager, commands := newTestUnicastManager(t)

	err := manager.Apply(t.Context(), []netip.Addr{
		mustUnicastAddress(t, "10.20.0.12"),
		mustUnicastAddress(t, "10.20.0.11"),
	})
	if err != nil {
		t.Fatal(err)
	}

	contents, readErr := os.ReadFile(manager.configuration.PeersFilePath)
	if readErr != nil {
		t.Fatal(readErr)
	}
	if string(contents) != "10.20.0.11\n10.20.0.12\n" {
		t.Fatalf("peer file = %q", contents)
	}
	if texts := unicastCommandText(commands.runCommands); !strings.Contains(strings.Join(texts, ";"), "systemctl enable --now "+UnicastUnitName) {
		t.Fatalf("commands = %v, want an enable --now call", texts)
	}
}

func TestUnicastManagerApplyKeepsMulticastWithoutARemotePeer(t *testing.T) {
	manager, commands := newTestUnicastManager(t)

	// The controller sends every running host, so the local address alone
	// means the region holds no other host.
	err := manager.Apply(t.Context(), []netip.Addr{mustUnicastAddress(t, "10.20.0.11")})
	if err != nil {
		t.Fatal(err)
	}

	if _, statErr := os.Stat(manager.configuration.PeersFilePath); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("peer file = %v, want no file", statErr)
	}
	texts := unicastCommandText(commands.runCommands)
	joined := strings.Join(texts, ";")
	if strings.Contains(joined, "enable") {
		t.Fatalf("commands = %v, want no enable call", texts)
	}
	if !strings.Contains(joined, "systemctl disable --now "+UnicastUnitName) {
		t.Fatalf("commands = %v, want a disable --now call", texts)
	}
}

func TestUnicastManagerApplyLeavesAnUnchangedPeerFileAlone(t *testing.T) {
	manager, _ := newTestUnicastManager(t)

	peers := []netip.Addr{
		mustUnicastAddress(t, "10.20.0.11"),
		mustUnicastAddress(t, "10.20.0.12"),
	}
	if err := manager.Apply(t.Context(), peers); err != nil {
		t.Fatal(err)
	}

	// A rewrite would reset the mode to 0644, so a private mode proves that
	// the unchanged file was not replaced.
	if err := os.Chmod(manager.configuration.PeersFilePath, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := manager.Apply(t.Context(), peers); err != nil {
		t.Fatal(err)
	}

	information, statErr := os.Stat(manager.configuration.PeersFilePath)
	if statErr != nil {
		t.Fatal(statErr)
	}
	if information.Mode().Perm() != 0o600 {
		t.Fatalf("peer file mode = %o, want the untouched 0600 mode", information.Mode().Perm())
	}
}

func TestUnicastManagerApplyRejectsAnInvalidPeerSet(t *testing.T) {
	manager, commands := newTestUnicastManager(t)

	duplicate := []netip.Addr{
		mustUnicastAddress(t, "10.20.0.12"),
		mustUnicastAddress(t, "10.20.0.12"),
	}
	if err := manager.Apply(t.Context(), duplicate); !errors.Is(err, ErrInvalidUnicastPeers) {
		t.Fatalf("duplicate error = %v", err)
	}

	notIPv4 := []netip.Addr{netip.MustParseAddr("fdab::1")}
	if err := manager.Apply(t.Context(), notIPv4); !errors.Is(err, ErrInvalidUnicastPeers) {
		t.Fatalf("non-IPv4 error = %v", err)
	}

	if len(commands.runCommands) != 0 {
		t.Fatalf("commands = %v, want none before validation", commands.runCommands)
	}
}

func TestUnicastManagerApplyRequiresTheDaemonUnit(t *testing.T) {
	manager, _ := newTestUnicastManager(t)
	if err := os.Remove(manager.configuration.UnitFilePath); err != nil {
		t.Fatal(err)
	}

	err := manager.Apply(t.Context(), []netip.Addr{mustUnicastAddress(t, "10.20.0.12")})
	if !errors.Is(err, ErrUnicastUnitNotInstalled) {
		t.Fatalf("error = %v, want ErrUnicastUnitNotInstalled", err)
	}
}

func TestUnicastManagerDisableStopsTheDaemon(t *testing.T) {
	manager, commands := newTestUnicastManager(t)

	if err := manager.Disable(t.Context()); err != nil {
		t.Fatal(err)
	}

	texts := unicastCommandText(commands.runCommands)
	if len(texts) != 1 || texts[0] != "systemctl disable --now "+UnicastUnitName {
		t.Fatalf("commands = %v, want one disable --now call", texts)
	}
}

func TestUnicastManagerDisableIgnoresAMissingDaemonUnit(t *testing.T) {
	manager, commands := newTestUnicastManager(t)
	if err := os.Remove(manager.configuration.UnitFilePath); err != nil {
		t.Fatal(err)
	}

	if err := manager.Disable(t.Context()); err != nil {
		t.Fatal(err)
	}

	if len(commands.runCommands) != 0 {
		t.Fatalf("commands = %v, want none without a unit file", commands.runCommands)
	}
}

func TestUnicastManagerApplyReportsAnUplinkWithoutIPv4(t *testing.T) {
	manager, commands := newTestUnicastManager(t)
	commands.outputResults[uplinkAddressCommand] = "2: eno1.1878 inet6 fdab:1::1/64 scope global eno1.1878\n"

	err := manager.Apply(t.Context(), []netip.Addr{mustUnicastAddress(t, "10.20.0.12")})
	if err == nil || !strings.Contains(err.Error(), "no global IPv4 address") {
		t.Fatalf("error = %v, want a missing uplink address", err)
	}
}

func TestUnicastManagerApplyReportsAnUplinkReadFailure(t *testing.T) {
	manager, commands := newTestUnicastManager(t)
	commands.outputErrors[uplinkAddressCommand] = errors.New("no such device")

	err := manager.Apply(t.Context(), []netip.Addr{mustUnicastAddress(t, "10.20.0.12")})
	if err == nil || !strings.Contains(err.Error(), "read uplink address") {
		t.Fatalf("error = %v, want an uplink read failure", err)
	}
}

func TestNewUnicastManagerRequiresItsConfiguration(t *testing.T) {
	if _, err := NewUnicastManager(UnicastConfig{UplinkName: "eno1.1878"}); err == nil {
		t.Fatal("an empty peers file path was accepted")
	}
	if _, err := NewUnicastManager(UnicastConfig{PeersFilePath: "/var/lib/metal/unicast-peers"}); err == nil {
		t.Fatal("an empty uplink name was accepted")
	}
}

func TestNewUnicastManagerSelectsTheInstalledUnitByDefault(t *testing.T) {
	manager, err := NewUnicastManager(UnicastConfig{
		PeersFilePath: "/var/lib/metal/unicast-peers",
		UplinkName:    "eno1.1878",
	})
	if err != nil {
		t.Fatal(err)
	}

	want := filepath.Join("/etc/systemd/system", UnicastUnitName)
	if manager.configuration.UnitFilePath != want {
		t.Fatalf("unit file = %q, want %q", manager.configuration.UnitFilePath, want)
	}
}

func TestUnicastPeerFileContentsSortsAndTerminates(t *testing.T) {
	contents := unicastPeerFileContents([]netip.Addr{
		mustUnicastAddress(t, "10.20.0.12"),
		mustUnicastAddress(t, "10.20.0.11"),
	})

	if contents != "10.20.0.11\n10.20.0.12\n" {
		t.Fatalf("contents = %q", contents)
	}
	if unicastPeerFileContents(nil) != "" {
		t.Fatal("an empty peer set must render an empty file")
	}
}
