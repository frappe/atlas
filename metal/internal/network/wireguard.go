package network

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"

	"github.com/frappe/atlas/metal/internal/hostcmd"
)

// ErrInvalidPeers reports an invalid desired WireGuard peer set.
var ErrInvalidPeers = errors.New("network: invalid WireGuard peers")

// WireGuardPeer describes one desired peer.
type WireGuardPeer struct {
	Node      string `json:"node"`
	NodeID    uint32 `json:"node_id"`
	PublicKey string `json:"public_key"`
	Address   string `json:"address"`
}

// WireGuardConfig identifies the interface and persistent peer state.
type WireGuardConfig struct {
	InterfaceName string
	StatePath     string
}

// WireGuardManager owns the managed peer set for one WireGuard interface.
type WireGuardManager struct {
	configuration WireGuardConfig
	commands      wireGuardCommands
	mutex         sync.Mutex
}

// NewWireGuardManager creates a manager for one WireGuard interface.
func NewWireGuardManager(configuration WireGuardConfig) (*WireGuardManager, error) {
	if configuration.InterfaceName == "" {
		return nil, fmt.Errorf("WireGuard interface name is required")
	}
	if configuration.StatePath == "" {
		return nil, fmt.Errorf("WireGuard state path is required")
	}

	return newWireGuardManager(configuration, hostWireGuardCommands{}), nil
}

// Apply replaces the managed peer set with the explicit desired peer set.
func (manager *WireGuardManager) Apply(ctx context.Context, desired []WireGuardPeer) error {
	desired = append([]WireGuardPeer(nil), desired...)
	if err := validateWireGuardPeers(desired); err != nil {
		return err
	}

	manager.mutex.Lock()
	defer manager.mutex.Unlock()

	current, err := loadWireGuardPeers(manager.configuration.StatePath)
	if err != nil {
		return fmt.Errorf("load managed WireGuard peers: %w", err)
	}

	localPublicKey, err := manager.commands.Output(ctx, "wg", "show", manager.configuration.InterfaceName, "public-key")
	if err != nil {
		return fmt.Errorf("read local WireGuard public key: %w", err)
	}
	localPublicKey = strings.TrimSpace(localPublicKey)

	localAddress, err := manager.wireGuardAddress(ctx)
	if err != nil {
		return err
	}

	managedDesired := withoutLocalPeer(desired, localPublicKey)
	if err := manager.reconcile(ctx, current, managedDesired, localAddress); err != nil {
		return err
	}
	if err := saveWireGuardPeers(manager.configuration.StatePath, managedDesired); err != nil {
		return fmt.Errorf("save managed WireGuard peers: %w", err)
	}
	return nil
}

func newWireGuardManager(configuration WireGuardConfig, commands wireGuardCommands) *WireGuardManager {
	return &WireGuardManager{configuration: configuration, commands: commands}
}

func (manager *WireGuardManager) reconcile(ctx context.Context, current, desired []WireGuardPeer, localAddress netip.Addr) error {
	currentByNode := wireGuardPeersByNode(current)
	desiredByNode := wireGuardPeersByNode(desired)

	for node, peer := range currentByNode {
		if replacement, found := desiredByNode[node]; !found || replacement != peer {
			if err := manager.commands.Run(ctx, "wg", "set", manager.configuration.InterfaceName, "peer", peer.PublicKey, "remove"); err != nil {
				return fmt.Errorf("remove WireGuard peer %q: %w", node, err)
			}
		}
	}

	for node, peer := range desiredByNode {
		allowedAddress := peerWireGuardAddress(localAddress, peer.NodeID)
		if err := manager.commands.Run(
			ctx,
			"wg",
			"set",
			manager.configuration.InterfaceName,
			"peer",
			peer.PublicKey,
			"endpoint",
			peer.Address,
			"allowed-ips",
			allowedAddress.String()+"/128",
			"persistent-keepalive",
			"25",
		); err != nil {
			return fmt.Errorf("configure WireGuard peer %q: %w", node, err)
		}
	}

	return nil
}

func (manager *WireGuardManager) wireGuardAddress(ctx context.Context) (netip.Addr, error) {
	output, err := manager.commands.Output(ctx, "ip", "-6", "-o", "addr", "show", "dev", manager.configuration.InterfaceName, "scope", "global")
	if err != nil {
		return netip.Addr{}, fmt.Errorf("read WireGuard interface address: %w", err)
	}
	for _, line := range strings.Split(output, "\n") {
		fields := strings.Fields(line)
		for index, field := range fields {
			if field != "inet6" || index+1 >= len(fields) {
				continue
			}
			prefix, parseErr := netip.ParsePrefix(fields[index+1])
			if parseErr == nil && prefix.Addr().Is6() {
				return prefix.Addr(), nil
			}
		}
	}
	return netip.Addr{}, fmt.Errorf("WireGuard interface has no global IPv6 address")
}

func validateWireGuardPeers(peers []WireGuardPeer) error {
	nodes := make(map[string]struct{}, len(peers))
	nodeIDs := make(map[uint32]struct{}, len(peers))
	publicKeys := make(map[string]struct{}, len(peers))

	for _, peer := range peers {
		if peer.Node == "" || peer.PublicKey == "" || peer.Address == "" {
			return fmt.Errorf("%w: each peer requires node, public_key, and address", ErrInvalidPeers)
		}
		host, port, err := net.SplitHostPort(peer.Address)
		if err != nil || host == "" || !validWireGuardPort(port) {
			return fmt.Errorf("%w: peer %q has an invalid address", ErrInvalidPeers, peer.Node)
		}
		if _, found := nodes[peer.Node]; found {
			return fmt.Errorf("%w: duplicate node %q", ErrInvalidPeers, peer.Node)
		}
		if _, found := nodeIDs[peer.NodeID]; found {
			return fmt.Errorf("%w: duplicate node_id %d", ErrInvalidPeers, peer.NodeID)
		}
		if _, found := publicKeys[peer.PublicKey]; found {
			return fmt.Errorf("%w: duplicate public_key", ErrInvalidPeers)
		}

		nodes[peer.Node] = struct{}{}
		nodeIDs[peer.NodeID] = struct{}{}
		publicKeys[peer.PublicKey] = struct{}{}
	}

	return nil
}

func validWireGuardPort(port string) bool {
	value, err := strconv.ParseUint(port, 10, 16)
	return err == nil && value > 0
}

func peerWireGuardAddress(localAddress netip.Addr, nodeID uint32) netip.Addr {
	address := localAddress.As16()
	for index := 4; index < len(address); index++ {
		address[index] = 0
	}
	binary.BigEndian.PutUint32(address[12:], nodeID)
	return netip.AddrFrom16(address)
}

func wireGuardPeersByNode(peers []WireGuardPeer) map[string]WireGuardPeer {
	byNode := make(map[string]WireGuardPeer, len(peers))
	for _, peer := range peers {
		byNode[peer.Node] = peer
	}
	return byNode
}

func withoutLocalPeer(peers []WireGuardPeer, localPublicKey string) []WireGuardPeer {
	managed := make([]WireGuardPeer, 0, len(peers))
	for _, peer := range peers {
		if peer.PublicKey != localPublicKey {
			managed = append(managed, peer)
		}
	}
	return managed
}

func loadWireGuardPeers(path string) ([]WireGuardPeer, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}

	var peers []WireGuardPeer
	if err := json.Unmarshal(data, &peers); err != nil {
		return nil, err
	}
	return peers, nil
}

func saveWireGuardPeers(path string, peers []WireGuardPeer) error {
	data, err := json.Marshal(peers)
	if err != nil {
		return err
	}
	data = append(data, '\n')

	directory := filepath.Dir(path)
	if err := os.MkdirAll(directory, 0o750); err != nil {
		return err
	}
	file, err := os.CreateTemp(directory, ".wireguard-peers-*")
	if err != nil {
		return err
	}
	temporaryPath := file.Name()
	defer os.Remove(temporaryPath)

	if err := writeWireGuardPeerFile(file, data); err != nil {
		return err
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return err
	}

	directoryFile, err := os.Open(directory)
	if err != nil {
		return err
	}
	defer directoryFile.Close()
	return directoryFile.Sync()
}

func writeWireGuardPeerFile(file *os.File, data []byte) error {
	if err := file.Chmod(0o600); err != nil {
		file.Close()
		return err
	}
	if _, err := file.Write(data); err != nil {
		file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return err
	}
	return file.Close()
}

// wireGuardCommands runs host network commands.
type wireGuardCommands interface {
	Run(ctx context.Context, name string, arguments ...string) error
	Output(ctx context.Context, name string, arguments ...string) (string, error)
}

type hostWireGuardCommands struct{}

func (hostWireGuardCommands) Run(ctx context.Context, name string, arguments ...string) error {
	return hostcmd.Run(ctx, name, arguments...)
}

func (hostWireGuardCommands) Output(ctx context.Context, name string, arguments ...string) (string, error) {
	return hostcmd.Output(ctx, name, arguments...)
}
