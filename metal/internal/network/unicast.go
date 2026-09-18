package network

import (
	"context"
	"errors"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"

	"github.com/frappe/atlas/metal/internal/platform"
)

// ErrInvalidUnicastPeers reports an invalid desired unicast peer set.
var ErrInvalidUnicastPeers = errors.New("network: invalid unicast peers")

// ErrUnicastUnitNotInstalled reports a missing unicast daemon unit file.
var ErrUnicastUnitNotInstalled = errors.New("network: unicast daemon unit is not installed")

// UnicastUnitName is the systemd unit that runs the unicast daemon.
const UnicastUnitName = "atlas-wg-mesh-unicast.service"

// systemdUnitDirectory holds the unit files that the host installer writes.
const systemdUnitDirectory = "/etc/systemd/system"

// UnicastConfig identifies the peer file and the uplink of one host.
type UnicastConfig struct {
	// PeersFilePath holds the peer addresses that the daemon reads.
	PeersFilePath string
	// UplinkName names the interface whose IPv4 address identifies this host.
	UplinkName string
	// UnitFilePath is the daemon unit file. An empty path selects the unit the
	// host installer writes.
	UnitFilePath string
}

// UnicastManager owns the controller-driven unicast peer set and daemon state.
// The peer file is the interface to the daemon: the daemon attaches the
// unicast hooks, removes the multicast NDP filters, and reloads the peer list
// map whenever the file changes.
type UnicastManager struct {
	configuration UnicastConfig
	commands      unicastCommands
	mutex         sync.Mutex
}

// NewUnicastManager creates a manager for one host.
func NewUnicastManager(configuration UnicastConfig) (*UnicastManager, error) {
	if configuration.PeersFilePath == "" {
		return nil, fmt.Errorf("unicast peers file path is required")
	}
	if configuration.UplinkName == "" {
		return nil, fmt.Errorf("unicast uplink name is required")
	}
	if configuration.UnitFilePath == "" {
		configuration.UnitFilePath = filepath.Join(systemdUnitDirectory, UnicastUnitName)
	}

	return &UnicastManager{configuration: configuration, commands: hostUnicastCommands{}}, nil
}

// Apply replaces the peer file and runs the daemon. The controller sends every
// running host of the region, so this host drops its own address before it
// decides: a host without a remote peer keeps the multicast NDP filters,
// because unicast transport has no destination.
func (manager *UnicastManager) Apply(ctx context.Context, peers []netip.Addr) error {
	if err := validateUnicastPeers(peers); err != nil {
		return err
	}

	manager.mutex.Lock()
	defer manager.mutex.Unlock()

	remote, err := manager.withoutLocalPeer(ctx, peers)
	if err != nil {
		return err
	}
	if len(remote) == 0 {
		return manager.stop(ctx)
	}
	if err := manager.writePeerFile(peers); err != nil {
		return err
	}
	return manager.start(ctx)
}

// Disable stops the daemon, which restores the multicast NDP filters. A host
// without the installed unit is already in multicast mode.
func (manager *UnicastManager) Disable(ctx context.Context) error {
	manager.mutex.Lock()
	defer manager.mutex.Unlock()

	return manager.stop(ctx)
}

// start enables and starts the daemon unit. Both actions are idempotent, so a
// running daemon is left alone and a crashed daemon starts again.
func (manager *UnicastManager) start(ctx context.Context) error {
	if !manager.unitIsInstalled() {
		return fmt.Errorf("%w: %s; run the host installation again",
			ErrUnicastUnitNotInstalled, manager.configuration.UnitFilePath)
	}

	// A unit that exhausted its restart limit refuses a start until its failed
	// state is cleared. Clearing a state that does not exist cannot fail here.
	_ = manager.commands.Run(ctx, "systemctl", "reset-failed", UnicastUnitName)
	if err := manager.commands.Run(ctx, "systemctl", "enable", "--now", UnicastUnitName); err != nil {
		return fmt.Errorf("start unicast daemon: %w", err)
	}
	return nil
}

// stop disables and stops the daemon unit. A missing unit file means the
// installer never wrote it, so the host is already in multicast mode.
func (manager *UnicastManager) stop(ctx context.Context) error {
	if !manager.unitIsInstalled() {
		return nil
	}

	if err := manager.commands.Run(ctx, "systemctl", "disable", "--now", UnicastUnitName); err != nil {
		return fmt.Errorf("stop unicast daemon: %w", err)
	}
	return nil
}

// unitIsInstalled reports whether the host installer wrote the daemon unit.
func (manager *UnicastManager) unitIsInstalled() bool {
	_, err := os.Stat(manager.configuration.UnitFilePath)
	return err == nil
}

// writePeerFile replaces the peer file when its contents changed. The daemon
// watches the file modification time, so an unchanged file must not be
// rewritten.
func (manager *UnicastManager) writePeerFile(peers []netip.Addr) error {
	desired := unicastPeerFileContents(peers)

	current, err := os.ReadFile(manager.configuration.PeersFilePath)
	if err == nil && string(current) == desired {
		return nil
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("read unicast peers file: %w", err)
	}

	if err := platform.WriteFile(manager.configuration.PeersFilePath, []byte(desired), 0o644); err != nil {
		return fmt.Errorf("write unicast peers file: %w", err)
	}
	return nil
}

// withoutLocalPeer drops the uplink IPv4 address of this host from the peer
// set. The daemon drops it too, but a host that would keep no remote peer must
// not start the daemon.
func (manager *UnicastManager) withoutLocalPeer(ctx context.Context, peers []netip.Addr) ([]netip.Addr, error) {
	localAddress, err := manager.uplinkAddress(ctx)
	if err != nil {
		return nil, err
	}

	remote := make([]netip.Addr, 0, len(peers))
	for _, peer := range peers {
		if peer != localAddress {
			remote = append(remote, peer)
		}
	}
	return remote, nil
}

// uplinkAddress reads the first global IPv4 address of the uplink.
func (manager *UnicastManager) uplinkAddress(ctx context.Context) (netip.Addr, error) {
	output, err := manager.commands.Output(
		ctx, "ip", "-4", "-o", "addr", "show", "dev", manager.configuration.UplinkName, "scope", "global",
	)
	if err != nil {
		return netip.Addr{}, fmt.Errorf("read uplink address: %w", err)
	}
	for _, field := range strings.Fields(output) {
		if prefix, prefixErr := netip.ParsePrefix(field); prefixErr == nil && prefix.Addr().Is4() {
			return prefix.Addr(), nil
		}
	}
	return netip.Addr{}, fmt.Errorf("uplink %s has no global IPv4 address", manager.configuration.UplinkName)
}

// validateUnicastPeers rejects a peer that is not IPv4 and a duplicate
// address. The whole set is checked before anything is applied.
func validateUnicastPeers(peers []netip.Addr) error {
	seen := make(map[netip.Addr]struct{}, len(peers))
	for _, peer := range peers {
		if !peer.IsValid() || !peer.Is4() {
			return fmt.Errorf("%w: peer %q is not an IPv4 address", ErrInvalidUnicastPeers, peer)
		}
		if _, found := seen[peer]; found {
			return fmt.Errorf("%w: duplicate peer %s", ErrInvalidUnicastPeers, peer)
		}
		seen[peer] = struct{}{}
	}
	return nil
}

// unicastPeerFileContents renders the peer file: one IPv4 address per line, in
// ascending order, with one trailing newline.
func unicastPeerFileContents(peers []netip.Addr) string {
	if len(peers) == 0 {
		return ""
	}

	sorted := slices.Clone(peers)
	slices.SortFunc(sorted, func(left, right netip.Addr) int { return left.Compare(right) })

	lines := make([]string, len(sorted))
	for index, peer := range sorted {
		lines[index] = peer.String()
	}
	return strings.Join(lines, "\n") + "\n"
}

// unicastCommands runs host commands.
type unicastCommands interface {
	Run(ctx context.Context, name string, arguments ...string) error
	Output(ctx context.Context, name string, arguments ...string) (string, error)
}

// hostUnicastCommands runs the commands on this host. Tests replace it.
type hostUnicastCommands struct{}

// Run executes one host command.
func (hostUnicastCommands) Run(ctx context.Context, name string, arguments ...string) error {
	return platform.Run(ctx, name, arguments...)
}

// Output executes one host command and returns its stdout.
func (hostUnicastCommands) Output(ctx context.Context, name string, arguments ...string) (string, error) {
	return platform.Output(ctx, name, arguments...)
}
