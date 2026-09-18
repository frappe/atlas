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

// newWireGuardManager builds a manager over the given command runner.
func newWireGuardManager(configuration WireGuardConfig, commands wireGuardCommands) *WireGuardManager {
	return &WireGuardManager{configuration: configuration, commands: commands}
}

// reconcile removes peers that changed or disappeared, then configures every
// desired peer. A changed peer is removed first, because `wg set` cannot move a
// node to a new public key in place. Runtime `wg set` installs no system
// routes, so the manager also owns one /128 route per peer: the encapsulated
// tunnel traffic from the mesh reaches wg0 only through these routes.
func (manager *WireGuardManager) reconcile(ctx context.Context, current, desired []WireGuardPeer, localAddress netip.Addr) error {
	currentByNode := wireGuardPeersByNode(current)
	desiredByNode := wireGuardPeersByNode(desired)

	for node, peer := range currentByNode {
		if replacement, found := desiredByNode[node]; !found || replacement != peer {
			if err := manager.commands.Run(ctx, "wg", "set", manager.configuration.InterfaceName, "peer", peer.PublicKey, "remove"); err != nil {
				return fmt.Errorf("remove WireGuard peer %q: %w", node, err)
			}
			if err := manager.removePeerRoute(ctx, localAddress, peer); err != nil {
				return err
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
			peerKeepaliveSeconds,
		); err != nil {
			return fmt.Errorf("configure WireGuard peer %q: %w", node, err)
		}
		if err := manager.replacePeerRoute(ctx, allowedAddress); err != nil {
			return err
		}
	}

	return nil
}

// replacePeerRoute points one peer address at the WireGuard interface, so the
// encapsulated mesh traffic from the BPF hooks enters the interface that
// WireGuard encrypts. Replace is idempotent, so a rebooted host with a
// surviving peer state regains its routes on the next Apply.
func (manager *WireGuardManager) replacePeerRoute(ctx context.Context, address netip.Addr) error {
	if err := manager.commands.Run(ctx, "ip", "-6", "route", "replace", address.String()+"/128", "dev", manager.configuration.InterfaceName); err != nil {
		return fmt.Errorf("route WireGuard peer %s: %w", address, err)
	}
	return nil
}

// removePeerRoute drops the route of a removed peer. A route that is already
// absent is not an error: routes do not survive a reboot, but the managed
// peer state does, so the removal must tolerate finding nothing.
func (manager *WireGuardManager) removePeerRoute(ctx context.Context, localAddress netip.Addr, peer WireGuardPeer) error {
	address := peerWireGuardAddress(localAddress, peer.NodeID)

	present, err := manager.peerRouteExists(ctx, address)
	if err != nil {
		return err
	}
	if !present {
		return nil
	}

	if err := manager.commands.Run(ctx, "ip", "-6", "route", "del", address.String()+"/128", "dev", manager.configuration.InterfaceName); err != nil {
		return fmt.Errorf("unroute WireGuard peer %s: %w", address, err)
	}
	return nil
}

// peerRouteExists reports whether the peer route is installed.
func (manager *WireGuardManager) peerRouteExists(ctx context.Context, address netip.Addr) (bool, error) {
	output, err := manager.commands.Output(ctx, "ip", "-6", "route", "show", address.String()+"/128")
	if err != nil {
		return false, fmt.Errorf("inspect WireGuard peer route %s: %w", address, err)
	}
	return strings.TrimSpace(output) != "", nil
}

// wireGuardAddress reads the global IPv6 address of the local interface, which
// supplies the prefix every peer address is built from.
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

// validateWireGuardPeers rejects a set with a missing field, an unusable
// address, or a repeated node, node ID, or public key. The whole set is checked
// before anything is applied, so a bad entry never lands halfway.
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

// validWireGuardPort reports whether port is a usable UDP port.
func validWireGuardPort(port string) bool {
	value, err := strconv.ParseUint(port, 10, 16)
	return err == nil && value > 0
}

// peerWireGuardAddress builds a peer address from the local prefix and the node
// ID: the first 4 bytes are kept, and the node ID becomes the last 4.
func peerWireGuardAddress(localAddress netip.Addr, nodeID uint32) netip.Addr {
	address := localAddress.As16()
	for index := 4; index < len(address); index++ {
		address[index] = 0
	}
	binary.BigEndian.PutUint32(address[12:], nodeID)
	return netip.AddrFrom16(address)
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
