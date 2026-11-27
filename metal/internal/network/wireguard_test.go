package network

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strconv"
	"sync"
	"testing"
	"time"
)

type fakeWireGuardCommands struct {
	mutex           sync.Mutex
	activeRuns      int
	maximumRuns     int
	commandDelay    time.Duration
	runArguments    [][]string
	outputFailure   error
	routeShowOutput string
	links           string
	tunnelPeers     string
}

func (commands *fakeWireGuardCommands) Run(_ context.Context, name string, arguments ...string) error {
	commands.mutex.Lock()
	commands.activeRuns++
	if commands.activeRuns > commands.maximumRuns {
		commands.maximumRuns = commands.activeRuns
	}
	commands.runArguments = append(commands.runArguments, append([]string{name}, arguments...))
	commands.mutex.Unlock()

	time.Sleep(commands.commandDelay)

	commands.mutex.Lock()
	commands.activeRuns--
	commands.mutex.Unlock()
	return nil
}

func (commands *fakeWireGuardCommands) Output(_ context.Context, name string, arguments ...string) (string, error) {
	if commands.outputFailure != nil {
		return "", commands.outputFailure
	}
	if name == "wg" && arguments[len(arguments)-1] == "peers" {
		return commands.tunnelPeers, nil
	}
	if name == "wg" {
		return "local-key\n", nil
	}
	if len(arguments) >= 2 && arguments[0] == "-o" && arguments[1] == "link" {
		return "7: wg0: <POINTOPOINT,NOARP,UP,LOWER_UP> mtu 8940 qdisc noqueue state UNKNOWN\n" + commands.links, nil
	}
	if len(arguments) >= 2 && arguments[0] == "-6" && arguments[1] == "route" {
		return commands.routeShowOutput, nil
	}
	return "7: wg0 inet6 fdaa:1:2:3::1/64 scope global\n", nil
}

func TestValidateWireGuardPeersRejectsDuplicateIdentity(t *testing.T) {
	base := WireGuardPeer{Node: "node-1", MeshAddress: "fdab:1::1", PublicKey: "key-1", Address: "192.0.2.1:51820"}
	tests := []struct {
		name string
		peer WireGuardPeer
	}{
		{name: "node", peer: WireGuardPeer{Node: "node-1", MeshAddress: "fdab:1::2", PublicKey: "key-2", Address: "192.0.2.2:51820"}},
		{name: "mesh address", peer: WireGuardPeer{Node: "node-2", MeshAddress: "fdab:1::1", PublicKey: "key-2", Address: "192.0.2.2:51820"}},
		{name: "public key", peer: WireGuardPeer{Node: "node-2", MeshAddress: "fdab:1::2", PublicKey: "key-1", Address: "192.0.2.2:51820"}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if err := validateWireGuardPeers([]WireGuardPeer{base, test.peer}); !errors.Is(err, ErrInvalidPeers) {
				t.Fatalf("error = %v, want ErrInvalidPeers", err)
			}
		})
	}
}

var localPeer = WireGuardPeer{Node: "node-1", MeshAddress: "fdab:1::1", PublicKey: "local-key", Address: "192.0.2.1:51820"}

func TestTunnelPortsSpreadEveryHostPairOverBothBondLinks(t *testing.T) {
	bondLink := func(source, destination int) int {
		ports := (source>>8 ^ source ^ destination>>8 ^ destination) & 0xff
		return ports >> 1 & 1
	}
	for first := 1; first <= 30; first++ {
		for second := first + 1; second <= 30; second++ {
			links := map[int]bool{}
			for index := range 16 {
				links[bondLink(tunnelPort(fmt.Sprintf("fdab:1::%x", first), index), tunnelPort(fmt.Sprintf("fdab:1::%x", second), index))] = true
			}
			if len(links) != 2 {
				t.Fatalf("hosts %d and %d use one bond link", first, second)
			}
		}
	}
}

func TestWireGuardManagerPersistsAppliedPeers(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "wireguard-peers.json")
	commands := &fakeWireGuardCommands{}
	manager := newWireGuardManager(WireGuardConfig{InterfaceName: "wg0", StatePath: statePath}, commands)
	peer := WireGuardPeer{Node: "node-2", MeshAddress: "fdab:1::2", PublicKey: "key-2", Address: "192.0.2.2:51820"}

	if _, err := manager.Apply(context.Background(), []WireGuardPeer{localPeer, peer}, 2); err != nil {
		t.Fatal(err)
	}
	stored, err := loadWireGuardPeers(statePath)
	if err != nil {
		t.Fatal(err)
	}
	if len(stored) != 1 || !reflect.DeepEqual(stored[0], peer) {
		t.Fatalf("stored peers = %+v", stored)
	}
	information, err := os.Stat(statePath)
	if err != nil {
		t.Fatal(err)
	}
	if information.Mode().Perm() != 0o600 {
		t.Errorf("state mode = %o, want 600", information.Mode().Perm())
	}
}

func TestWireGuardManagerSerializesApplication(t *testing.T) {
	commands := &fakeWireGuardCommands{commandDelay: 20 * time.Millisecond}
	manager := newWireGuardManager(WireGuardConfig{
		InterfaceName: "wg0",
		StatePath:     filepath.Join(t.TempDir(), "wireguard-peers.json"),
	}, commands)
	peers := []WireGuardPeer{{Node: "node-2", MeshAddress: "fdab:1::2", PublicKey: "key-2", Address: "192.0.2.2:51820"}}

	var waitGroup sync.WaitGroup
	for range 2 {
		waitGroup.Add(1)
		go func() {
			defer waitGroup.Done()
			if _, err := manager.Apply(context.Background(), append(peers, localPeer), 2); err != nil {
				t.Errorf("Apply: %v", err)
			}
		}()
	}
	waitGroup.Wait()

	if commands.maximumRuns != 1 {
		t.Fatalf("maximum concurrent commands = %d, want 1", commands.maximumRuns)
	}
}

func TestWireGuardManagerInstallsPeerRoutes(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "wireguard-peers.json")
	commands := &fakeWireGuardCommands{}
	manager := newWireGuardManager(WireGuardConfig{InterfaceName: "wg0", StatePath: statePath}, commands)
	peer := WireGuardPeer{Node: "node-2", MeshAddress: "fdab:1::2", PublicKey: "key-2", Address: "192.0.2.2:51820"}

	if _, err := manager.Apply(context.Background(), []WireGuardPeer{localPeer, peer}, 2); err != nil {
		t.Fatal(err)
	}

	runs := recordedWireGuardRuns(commands)
	if !wireGuardRunRecorded(runs, []string{"ip", "-6", "route", "replace", "fdab:1::2/128", "nexthop", "via", "fe80::1", "dev", "wg1", "onlink", "nexthop", "via", "fe80::1", "dev", "wg2", "onlink"}) {
		t.Fatalf("runs = %v, want a multipath route over the tunnels", runs)
	}
}

func TestWireGuardManagerCreatesTunnelsAndRemovesStaleOnes(t *testing.T) {
	commands := &fakeWireGuardCommands{links: "9: wg1: <UP> mtu 8940\n10: wg3: <UP> mtu 8940\n", tunnelPeers: "stale-key\n"}
	manager := newWireGuardManager(WireGuardConfig{InterfaceName: "wg0", StatePath: filepath.Join(t.TempDir(), "wireguard-peers.json")}, commands)
	peer := WireGuardPeer{Node: "node-2", MeshAddress: "fdab:1::2", PublicKey: "key-2", Address: "192.0.2.2:51820"}

	tunnels, err := manager.Apply(context.Background(), []WireGuardPeer{localPeer, peer}, 2)
	if err != nil {
		t.Fatal(err)
	}

	runs := recordedWireGuardRuns(commands)
	if !slices.Equal(tunnels, []string{"wg1", "wg2"}) {
		t.Fatalf("tunnels = %v, want wg1 and wg2", tunnels)
	}
	for _, want := range [][]string{
		{"ip", "link", "add", "dev", "wg2", "type", "wireguard"},
		{"ip", "link", "set", "dev", "wg2", "mtu", "8940", "up"},
		{"ip", "link", "set", "dev", "wg1", "mtu", "8940", "up"},
		{"ip", "link", "del", "dev", "wg3"},
		{"wg", "set", "wg2", "private-key", "/etc/wireguard/wg0.key", "listen-port", strconv.Itoa(tunnelPort("fdab:1::1", 1)),
			"peer", "key-2", "endpoint", "192.0.2.2:" + strconv.Itoa(tunnelPort("fdab:1::2", 1)), "allowed-ips", "fdab:1::2/128", "persistent-keepalive", "25",
			"peer", "stale-key", "remove"},
	} {
		if !wireGuardRunRecorded(runs, want) {
			t.Fatalf("runs = %v, want %v", runs, want)
		}
	}
	if wireGuardRunRecorded(runs, []string{"ip", "link", "add", "dev", "wg1", "type", "wireguard"}) {
		t.Fatalf("runs = %v, want the existing wg1 kept", runs)
	}
}

func wireGuardRunRecorded(runs [][]string, want []string) bool {
	return slices.ContainsFunc(runs, func(run []string) bool {
		return slices.Equal(run, want)
	})
}

func recordedWireGuardRuns(commands *fakeWireGuardCommands) [][]string {
	commands.mutex.Lock()
	defer commands.mutex.Unlock()
	return append([][]string(nil), commands.runArguments...)
}
