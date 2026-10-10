package network

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"strconv"
	"strings"
	"sync"

	"github.com/frappe/atlas/metal/internal/platform"
)

// peerKeepaliveSeconds keeps a NAT mapping open between peers that are otherwise
// idle, so a peer behind NAT stays reachable.
const peerKeepaliveSeconds = "25"

// ErrInvalidPeers reports an invalid desired WireGuard peer set.
var ErrInvalidPeers = errors.New("network: invalid WireGuard peers")

// WireGuardPeer describes one desired peer. MeshAddress is the peer's address
// on the mesh, which Atlas owns and sends, and which becomes its AllowedIPs.
type WireGuardPeer struct {
	Node              string `json:"node"`
	MeshAddress       string `json:"mesh_address"`
	PublicKey         string `json:"public_key"`
	Address           string `json:"address"`
	PublicAddress     string `json:"public_address"`
	PrivateAddress    string `json:"private_address"`
	PrivateNetworkMAC string `json:"private_network_mac_address"`
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

	managedDesired := withoutLocalPeer(desired, localPublicKey)
	if err := manager.reconcile(ctx, current, managedDesired); err != nil {
		return err
	}
	if err := saveWireGuardPeers(manager.configuration.StatePath, managedDesired); err != nil {
		return fmt.Errorf("save managed WireGuard peers: %w", err)
	}
	return nil
}

// newWireGuardManager builds a manager over the given command runner.
func newWireGuardManager(configuration WireGuardConfig, commands wireGuardCommands) *WireGuardManager {
	return &WireGuardManager{configuration: configuration, commands: commands}
}

// reconcile removes peers that disappeared or changed key or mesh address, then
// configures every desired peer. `wg set` updates an endpoint in place, but it
// cannot move a node to a new public key.
func (manager *WireGuardManager) reconcile(ctx context.Context, current, desired []WireGuardPeer) error {
	currentByNode := wireGuardPeersByNode(current)
	desiredByNode := wireGuardPeersByNode(desired)

	for node, peer := range currentByNode {
		replacement, found := desiredByNode[node]
		if !found || replacement.PublicKey != peer.PublicKey || replacement.MeshAddress != peer.MeshAddress {
			if err := manager.commands.Run(ctx, "wg", "set", manager.configuration.InterfaceName, "peer", peer.PublicKey, "remove"); err != nil {
				return fmt.Errorf("remove WireGuard peer %q: %w", node, err)
			}
			if err := manager.removePeerRoute(ctx, peer); err != nil {
				return err
			}
		}
	}

	for node, peer := range desiredByNode {
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
			peer.MeshAddress+"/128",
			"persistent-keepalive",
			peerKeepaliveSeconds,
		); err != nil {
			return fmt.Errorf("configure WireGuard peer %q: %w", node, err)
		}
		if err := manager.replacePeerRoute(ctx, peer); err != nil {
			return err
		}
	}

	return nil
}

// replacePeerRoute points one peer mesh address at the WireGuard interface.
func (manager *WireGuardManager) replacePeerRoute(ctx context.Context, peer WireGuardPeer) error {
	if err := manager.commands.Run(ctx, "ip", "-6", "route", "replace", peer.MeshAddress+"/128", "dev", manager.configuration.InterfaceName); err != nil {
		return fmt.Errorf("route WireGuard peer %s: %w", peer.MeshAddress, err)
	}
	return nil
}

// removePeerRoute drops the route of a removed peer.
func (manager *WireGuardManager) removePeerRoute(ctx context.Context, peer WireGuardPeer) error {
	present, err := manager.commands.Output(ctx, "ip", "-6", "route", "show", peer.MeshAddress+"/128")
	if err != nil {
		return fmt.Errorf("inspect WireGuard peer route %s: %w", peer.MeshAddress, err)
	}
	if strings.TrimSpace(present) == "" {
		return nil
	}

	if err := manager.commands.Run(ctx, "ip", "-6", "route", "del", peer.MeshAddress+"/128", "dev", manager.configuration.InterfaceName); err != nil {
		return fmt.Errorf("unroute WireGuard peer %s: %w", peer.MeshAddress, err)
	}
	return nil
}

// validateWireGuardPeers rejects a set with a missing field, an unusable
// address, or a repeated node, mesh address, or public key. The whole set is
// checked before anything is applied, so a bad entry never lands halfway.
func validateWireGuardPeers(peers []WireGuardPeer) error {
	nodes := make(map[string]struct{}, len(peers))
	meshAddresses := make(map[netip.Addr]struct{}, len(peers))
	publicKeys := make(map[string]struct{}, len(peers))

	for _, peer := range peers {
		if peer.Node == "" || peer.PublicKey == "" || peer.Address == "" || peer.MeshAddress == "" {
			return fmt.Errorf("%w: each peer requires node, mesh_address, public_key, and address", ErrInvalidPeers)
		}
		meshAddress, err := netip.ParseAddr(peer.MeshAddress)
		if err != nil || !meshAddress.Is6() {
			return fmt.Errorf("%w: peer %q has an invalid mesh address", ErrInvalidPeers, peer.Node)
		}
		host, port, err := net.SplitHostPort(peer.Address)
		if err != nil || host == "" || !validWireGuardPort(port) {
			return fmt.Errorf("%w: peer %q has an invalid address", ErrInvalidPeers, peer.Node)
		}
		if _, found := nodes[peer.Node]; found {
			return fmt.Errorf("%w: duplicate node %q", ErrInvalidPeers, peer.Node)
		}
		if _, found := meshAddresses[meshAddress]; found {
			return fmt.Errorf("%w: duplicate mesh_address %s", ErrInvalidPeers, meshAddress)
		}
		if _, found := publicKeys[peer.PublicKey]; found {
			return fmt.Errorf("%w: duplicate public_key", ErrInvalidPeers)
		}

		nodes[peer.Node] = struct{}{}
		meshAddresses[meshAddress] = struct{}{}
		publicKeys[peer.PublicKey] = struct{}{}
	}

	return nil
}

// validWireGuardPort reports whether port is a usable UDP port.
func validWireGuardPort(port string) bool {
	value, err := strconv.ParseUint(port, 10, 16)
	return err == nil && value > 0
}

// wireGuardPeersByNode indexes a peer set by node name.
func wireGuardPeersByNode(peers []WireGuardPeer) map[string]WireGuardPeer {
	byNode := make(map[string]WireGuardPeer, len(peers))
	for _, peer := range peers {
		byNode[peer.Node] = peer
	}
	return byNode
}

// withoutLocalPeer drops this host from a peer set. A host must not peer with
// itself, and the controller sends the whole fleet.
func withoutLocalPeer(peers []WireGuardPeer, localPublicKey string) []WireGuardPeer {
	managed := make([]WireGuardPeer, 0, len(peers))
	for _, peer := range peers {
		if peer.PublicKey != localPublicKey {
			managed = append(managed, peer)
		}
	}
	return managed
}

// loadWireGuardPeers reads the peers this manager last applied. An absent file
// means none, which is the correct state for a fresh host.
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

// saveWireGuardPeers records the applied set, so the next Apply knows what it
// owns and leaves peers added by other tools alone.
func saveWireGuardPeers(path string, peers []WireGuardPeer) error {
	data, err := json.Marshal(peers)
	if err != nil {
		return err
	}
	data = append(data, '\n')

	return platform.WriteFile(path, data, 0o600)
}

// wireGuardCommands runs host network commands.
type wireGuardCommands interface {
	Run(ctx context.Context, name string, arguments ...string) error
	Output(ctx context.Context, name string, arguments ...string) (string, error)
}

// hostWireGuardCommands runs the commands on this host. Tests replace it.
type hostWireGuardCommands struct{}

// Run executes one host command.
func (hostWireGuardCommands) Run(ctx context.Context, name string, arguments ...string) error {
	return platform.Run(ctx, name, arguments...)
}

// Output executes one host command and returns its stdout.
func (hostWireGuardCommands) Output(ctx context.Context, name string, arguments ...string) (string, error) {
	return platform.Output(ctx, name, arguments...)
}
