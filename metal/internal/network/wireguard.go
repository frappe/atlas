package network

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"

	"github.com/frappe/atlas/metal/internal/platform"
)

// peerKeepaliveSeconds keeps a NAT mapping open between peers that are otherwise
// idle, so a peer behind NAT stays reachable.
const peerKeepaliveSeconds = "25"

// Mesh tunnel K listens on tunnelBasePort + 4*(K-1), plus 2 when bit K-1 of
// the host's port bits is set. The bond hash XORs both ports, and bit 1 picks
// the link, so a host pair spreads its tunnels over both links.
const (
	tunnelBasePort = 51840
	tunnelLimit    = 32
)

var wireGuardLinkPattern = regexp.MustCompile(`(?m)^\d+: (wg\d+): .* mtu (\d+)`)

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

// Apply creates the mesh tunnels wg1 to wgN, sets the desired peers on them,
// and routes each peer over them. It returns the tunnel names. Atlas owns wg0.
func (manager *WireGuardManager) Apply(ctx context.Context, desired []WireGuardPeer, tunnelCount int) ([]string, error) {
	desired = append([]WireGuardPeer(nil), desired...)
	if err := validateWireGuardPeers(desired); err != nil {
		return nil, err
	}
	if tunnelCount < 1 || tunnelCount > tunnelLimit {
		return nil, fmt.Errorf("%w: tunnel count %d is not from 1 to %d", ErrInvalidPeers, tunnelCount, tunnelLimit)
	}

	manager.mutex.Lock()
	defer manager.mutex.Unlock()

	current, err := loadWireGuardPeers(manager.configuration.StatePath)
	if err != nil {
		return nil, fmt.Errorf("load managed WireGuard peers: %w", err)
	}

	localPublicKey, err := manager.commands.Output(ctx, "wg", "show", manager.configuration.InterfaceName, "public-key")
	if err != nil {
		return nil, fmt.Errorf("read local WireGuard public key: %w", err)
	}
	localPublicKey = strings.TrimSpace(localPublicKey)

	managedDesired := withoutLocalPeer(desired, localPublicKey)
	if len(managedDesired) == len(desired) {
		return nil, fmt.Errorf("%w: the peers do not include this host", ErrInvalidPeers)
	}
	local := slices.IndexFunc(desired, func(peer WireGuardPeer) bool { return peer.PublicKey == localPublicKey })
	tunnels, err := manager.ensureTunnels(ctx, tunnelCount)
	if err != nil {
		return nil, err
	}
	for index, name := range tunnels {
		if err := manager.setTunnelPeers(ctx, name, index, desired[local].MeshAddress, managedDesired); err != nil {
			return nil, err
		}
	}
	if err := manager.reconcile(ctx, current, managedDesired, tunnels); err != nil {
		return nil, err
	}
	if err := saveWireGuardPeers(manager.configuration.StatePath, managedDesired); err != nil {
		return nil, fmt.Errorf("save managed WireGuard peers: %w", err)
	}
	return tunnels, nil
}

// ensureTunnels creates the missing tunnels with the wg0 MTU and deletes those
// above the count.
func (manager *WireGuardManager) ensureTunnels(ctx context.Context, count int) ([]string, error) {
	output, err := manager.commands.Output(ctx, "ip", "-o", "link", "show", "type", "wireguard")
	if err != nil {
		return nil, fmt.Errorf("list WireGuard links: %w", err)
	}
	mtus := make(map[string]string)
	for _, match := range wireGuardLinkPattern.FindAllStringSubmatch(output, -1) {
		mtus[match[1]] = match[2]
	}
	if mtus[manager.configuration.InterfaceName] == "" {
		return nil, fmt.Errorf("WireGuard interface %s is missing", manager.configuration.InterfaceName)
	}

	tunnels := make([]string, count)
	for index := range tunnels {
		tunnels[index] = fmt.Sprintf("wg%d", index+1)
		if mtus[tunnels[index]] == "" {
			if err := manager.commands.Run(ctx, "ip", "link", "add", "dev", tunnels[index], "type", "wireguard"); err != nil {
				return nil, fmt.Errorf("create mesh tunnel %s: %w", tunnels[index], err)
			}
		}
		if err := manager.commands.Run(ctx, "ip", "link", "set", "dev", tunnels[index], "mtu", mtus[manager.configuration.InterfaceName], "up"); err != nil {
			return nil, fmt.Errorf("start mesh tunnel %s: %w", tunnels[index], err)
		}
	}

	for name := range mtus {
		number, _ := strconv.Atoi(strings.TrimPrefix(name, "wg"))
		if number > count {
			if err := manager.commands.Run(ctx, "ip", "link", "del", "dev", name); err != nil {
				return nil, fmt.Errorf("delete mesh tunnel %s: %w", name, err)
			}
		}
	}
	return tunnels, nil
}

// setTunnelPeers sets the key, port and peers of one tunnel, and removes every
// other peer from it.
func (manager *WireGuardManager) setTunnelPeers(ctx context.Context, name string, index int, localMeshAddress string, peers []WireGuardPeer) error {
	present, err := manager.commands.Output(ctx, "wg", "show", name, "peers")
	if err != nil {
		return fmt.Errorf("list peers of %s: %w", name, err)
	}

	arguments := []string{"set", name, "private-key", "/etc/wireguard/" + manager.configuration.InterfaceName + ".key", "listen-port", strconv.Itoa(tunnelPort(localMeshAddress, index))}
	desiredKeys := make(map[string]bool, len(peers))
	for _, peer := range peers {
		host, _, _ := net.SplitHostPort(peer.Address)
		endpoint := net.JoinHostPort(host, strconv.Itoa(tunnelPort(peer.MeshAddress, index)))
		arguments = append(arguments, "peer", peer.PublicKey, "endpoint", endpoint, "allowed-ips", peer.MeshAddress+"/128", "persistent-keepalive", peerKeepaliveSeconds)
		desiredKeys[peer.PublicKey] = true
	}
	for _, key := range strings.Fields(present) {
		if !desiredKeys[key] {
			arguments = append(arguments, "peer", key, "remove")
		}
	}

	if err := manager.commands.Run(ctx, "wg", arguments...); err != nil {
		return fmt.Errorf("set peers of %s: %w", name, err)
	}
	return nil
}

func tunnelPort(meshAddress string, index int) int {
	digest := sha256.Sum256([]byte(meshAddress))
	bits := binary.BigEndian.Uint32(digest[:4])
	return tunnelBasePort + 4*index + 2*int(bits>>index&1)
}

// newWireGuardManager builds a manager over the given command runner.
func newWireGuardManager(configuration WireGuardConfig, commands wireGuardCommands) *WireGuardManager {
	return &WireGuardManager{configuration: configuration, commands: commands}
}

// reconcile removes the routes of peers that disappeared or changed mesh
// address, then routes every desired peer over the tunnels.
func (manager *WireGuardManager) reconcile(ctx context.Context, current, desired []WireGuardPeer, tunnels []string) error {
	currentByNode := wireGuardPeersByNode(current)
	desiredByNode := wireGuardPeersByNode(desired)

	for node, peer := range currentByNode {
		replacement, found := desiredByNode[node]
		if !found || replacement.MeshAddress != peer.MeshAddress {
			if err := manager.removePeerRoute(ctx, peer); err != nil {
				return err
			}
		}
	}

	for _, peer := range desiredByNode {
		if err := manager.replacePeerRoute(ctx, peer, tunnels); err != nil {
			return err
		}
	}

	return nil
}

// replacePeerRoute gives each tunnel an on-link gateway because Linux rejects
// device-only IPv6 multipath routes. WireGuard ignores the gateway.
func (manager *WireGuardManager) replacePeerRoute(ctx context.Context, peer WireGuardPeer, tunnels []string) error {
	arguments := []string{"-6", "route", "replace", peer.MeshAddress + "/128"}
	for _, name := range tunnels {
		arguments = append(arguments, "nexthop", "via", "fe80::1", "dev", name, "onlink")
	}
	if err := manager.commands.Run(ctx, "ip", arguments...); err != nil {
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

	if err := manager.commands.Run(ctx, "ip", "-6", "route", "del", peer.MeshAddress+"/128"); err != nil {
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
