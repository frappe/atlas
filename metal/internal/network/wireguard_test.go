package network

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

type fakeWireGuardCommands struct {
	mutex         sync.Mutex
	activeRuns    int
	maximumRuns   int
	commandDelay  time.Duration
	runArguments  [][]string
	outputFailure error
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
	if name == "wg" {
		return "local-key\n", nil
	}
	return "7: wg0 inet6 fdaa:1:2:3::1/64 scope global\n", nil
}

func TestValidateWireGuardPeersRejectsDuplicateIdentity(t *testing.T) {
	base := WireGuardPeer{Node: "node-1", NodeID: 1, PublicKey: "key-1", Address: "192.0.2.1:51820"}
	tests := []struct {
		name string
		peer WireGuardPeer
	}{
		{name: "node", peer: WireGuardPeer{Node: "node-1", NodeID: 2, PublicKey: "key-2", Address: "192.0.2.2:51820"}},
		{name: "node ID", peer: WireGuardPeer{Node: "node-2", NodeID: 1, PublicKey: "key-2", Address: "192.0.2.2:51820"}},
		{name: "public key", peer: WireGuardPeer{Node: "node-2", NodeID: 2, PublicKey: "key-1", Address: "192.0.2.2:51820"}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if err := validateWireGuardPeers([]WireGuardPeer{base, test.peer}); !errors.Is(err, ErrInvalidPeers) {
				t.Fatalf("error = %v, want ErrInvalidPeers", err)
			}
		})
	}
}

func TestWireGuardManagerPersistsAppliedPeers(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "wireguard-peers.json")
	commands := &fakeWireGuardCommands{}
	manager := newWireGuardManager(WireGuardConfig{InterfaceName: "wg0", StatePath: statePath}, commands)
	peer := WireGuardPeer{Node: "node-2", NodeID: 2, PublicKey: "key-2", Address: "192.0.2.2:51820"}

	if err := manager.Apply(context.Background(), []WireGuardPeer{peer}); err != nil {
		t.Fatal(err)
	}
	stored, err := loadWireGuardPeers(statePath)
	if err != nil {
		t.Fatal(err)
	}
	if len(stored) != 1 || stored[0] != peer {
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
	peers := []WireGuardPeer{{Node: "node-2", NodeID: 2, PublicKey: "key-2", Address: "192.0.2.2:51820"}}

	var waitGroup sync.WaitGroup
	for range 2 {
		waitGroup.Add(1)
		go func() {
			defer waitGroup.Done()
			if err := manager.Apply(context.Background(), peers); err != nil {
				t.Errorf("Apply: %v", err)
			}
		}()
	}
	waitGroup.Wait()

	if commands.maximumRuns != 1 {
		t.Fatalf("maximum concurrent commands = %d, want 1", commands.maximumRuns)
	}
}
