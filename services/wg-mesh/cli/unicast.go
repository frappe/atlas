package main

import (
	"bufio"
	"errors"
	"fmt"
	"net/netip"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/spf13/cobra"
)

// Must match ATLAS_UNICAST_PEER_LIMIT in bpf/state.h and the peer_list map.
const unicastPeerLimit = 256

const (
	unicastLockPath = "/run/lock/atlas-wg-mesh-unicast.lock"

	// Filter priorities on the uplink. A start or stop passes through a short
	// window with both hook sets attached: ingress unicast decaps before the
	// multicast hook at priority 10, and egress unicast wraps after it.
	unicastIngressFilterPriority = "5"
	unicastEgressFilterPriority  = "15"
)

var unicastVerbose bool

var unicastCommand = &cobra.Command{
	Use:   "unicast",
	Short: "transport NDP over a routed IPv4 underlay",
}

var unicastPeerCommand = &cobra.Command{
	Use:   "peer",
	Short: "manage the unicast peer file",
}

var unicastPeerAddCommand = &cobra.Command{
	Use:   "add PEERS_FILE PEER",
	Short: "add one peer IPv4 address to the peer file",
	Args:  cobra.ExactArgs(2),
	RunE: func(_ *cobra.Command, arguments []string) error {
		return updateUnicastPeerFile(arguments[0], arguments[1], true)
	},
}

var unicastPeerRemoveCommand = &cobra.Command{
	Use:   "remove PEERS_FILE PEER",
	Short: "remove one peer IPv4 address from the peer file",
	Args:  cobra.ExactArgs(2),
	RunE: func(_ *cobra.Command, arguments []string) error {
		return updateUnicastPeerFile(arguments[0], arguments[1], false)
	},
}

var unicastPeerListCommand = &cobra.Command{
	Use:   "list PEERS_FILE",
	Short: "print every peer IPv4 address in the peer file",
	Args:  cobra.ExactArgs(1),
	RunE: func(_ *cobra.Command, arguments []string) error {
		return listUnicastPeerEntries(arguments[0])
	},
}

var unicastStartCommand = &cobra.Command{
	Use:   "start PEERS_FILE",
	Short: "attach the unicast hooks and keep the peer list map in sync",
	Args:  cobra.ExactArgs(1),
	RunE: func(_ *cobra.Command, arguments []string) error {
		return runUnicastDaemon(arguments[0], unicastVerbose)
	},
}

// parseUnicastPeerAddress converts one text address to a strict IPv4 peer.
func parseUnicastPeerAddress(peerText string) (netip.Addr, error) {
	peer, err := netip.ParseAddr(peerText)
	if err != nil || !peer.Is4() {
		return netip.Addr{}, fmt.Errorf("%q is not an IPv4 address", peerText)
	}
	return peer, nil
}

// readUnicastPeerEntries reads the peer file as written: one IPv4 address
// per non-comment line. The file can hold the local host address.
func readUnicastPeerEntries(path string) ([]netip.Addr, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	entries := make([]netip.Addr, 0)
	seen := make(map[netip.Addr]bool)
	scanner := bufio.NewScanner(file)
	line := 0
	for scanner.Scan() {
		line++
		value := strings.TrimSpace(strings.SplitN(scanner.Text(), "#", 2)[0])
		if value == "" {
			continue
		}
		peer, err := parseUnicastPeerAddress(value)
		if err != nil {
			return nil, fmt.Errorf("%s:%d: %w", path, line, err)
		}
		if seen[peer] {
			continue
		}
		seen[peer] = true
		entries = append(entries, peer)
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	if len(entries) > unicastPeerLimit {
		return nil, fmt.Errorf("%s holds more than %d peers", path, unicastPeerLimit)
	}
	return entries, nil
}

// writeUnicastPeerEntries replaces the peer file atomically, so a reader
// never observes a partial edit.
func writeUnicastPeerEntries(path string, entries []netip.Addr) error {
	temporary, err := os.CreateTemp(filepath.Dir(path), ".atlas-unicast-peers-")
	if err != nil {
		return err
	}
	defer os.Remove(temporary.Name())

	writer := bufio.NewWriter(temporary)
	for _, peer := range entries {
		if _, err := fmt.Fprintln(writer, peer.String()); err != nil {
			temporary.Close()
			return err
		}
	}
	if err := writer.Flush(); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Chmod(0644); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	return os.Rename(temporary.Name(), path)
}

// updateUnicastPeerFile adds or removes one peer address. Both directions
// are safe to repeat.
func updateUnicastPeerFile(path string, peerText string, add bool) error {
	peer, err := parseUnicastPeerAddress(peerText)
	if err != nil {
		return err
	}

	entries, err := readUnicastPeerEntries(path)
	if err != nil && !(add && errors.Is(err, os.ErrNotExist)) {
		return err
	}

	present := false
	updated := make([]netip.Addr, 0, len(entries)+1)
	for _, entry := range entries {
		if entry == peer {
			present = true
			continue
		}
		updated = append(updated, entry)
	}

	if add {
		updated = append(updated, peer)
	}

	if present == add {
		fmt.Printf("%s is already %s\n", peer, unicastPeerState(add))
		return nil
	}

	if len(updated) > unicastPeerLimit {
		return fmt.Errorf("the peer file cannot hold more than %d peers", unicastPeerLimit)
	}

	if err := writeUnicastPeerEntries(path, updated); err != nil {
		return err
	}
	fmt.Printf("%s %s\n", peer, unicastPeerState(add))
	return nil
}

func unicastPeerState(add bool) string {
	if add {
		return "added"
	}
	return "removed"
}

// listUnicastPeerEntries prints one peer address per line.
func listUnicastPeerEntries(path string) error {
	entries, err := readUnicastPeerEntries(path)
	if err != nil {
		return err
	}
	for _, peer := range entries {
		fmt.Println(peer.String())
	}
	return nil
}

// unicastTransportPeers drops the local host, which would otherwise receive
// its own fan-out copies.
func unicastTransportPeers(entries []netip.Addr, self [4]byte) []netip.Addr {
	peers := make([]netip.Addr, 0, len(entries))
	for _, peer := range entries {
		if peer == netip.AddrFrom4(self) {
			continue
		}
		peers = append(peers, peer)
	}
	return peers
}

// unicastPeerMapValues packs the peers densely from index zero. The BPF
// fan-out stops at the first zero value, so empty slots must hold zero.
func unicastPeerMapValues(peers []netip.Addr) [][4]byte {
	values := make([][4]byte, unicastPeerLimit)
	for index, peer := range peers {
		values[index] = peer.As4()
	}
	return values
}

// syncUnicastPeerMap rewrites the whole peer_list map, so removed peers
// cannot stay behind in a slot.
func syncUnicastPeerMap(entries []netip.Addr, self [4]byte) error {
	peerMap, err := openMap("peer_list")
	if err != nil {
		return err
	}
	defer peerMap.Close()

	for index, value := range unicastPeerMapValues(unicastTransportPeers(entries, self)) {
		if err := peerMap.Put(uint32(index), value); err != nil {
			return err
		}
	}
	return nil
}

// runUnicastDaemon switches the uplink to unicast NDP transport and keeps
// the peer_list map in step with the peer file. The BPF hooks do the
// packet processing.
//
// On start, the daemon attaches the unicast hooks and removes the multicast
// NDP filters from the uplink. On a clean stop, it restores the multicast
// filters first, so neighbour discovery never stops. A failed start leaves
// the multicast filters in place.
func runUnicastDaemon(path string, verbose bool) error {
	config, err := readPinnedConfig()
	if err != nil {
		return err
	}
	uplinkName := interfaceWithIPv4(config.UplinkIPv4)
	if uplinkName == "" {
		return errors.New("cannot find the configured uplink")
	}
	entries, err := readUnicastPeerEntries(path)
	if err != nil {
		return err
	}
	peers := unicastTransportPeers(entries, config.UplinkIPv4)
	if len(peers) == 0 {
		return fmt.Errorf("%s contains no remote peers", path)
	}
	information, err := os.Stat(path)
	if err != nil {
		return err
	}

	unlock, err := lockUnicastDaemon()
	if err != nil {
		return err
	}
	defer unlock()

	if err := syncUnicastPeerMap(entries, config.UplinkIPv4); err != nil {
		return err
	}
	logUnicastSync(verbose, peers)

	if err := installTransportNeighbours(peers, uplinkName); err != nil {
		return err
	}

	if err := attachUnicastHook(uplinkName, ndpUnicastIngressProgram, "ingress", unicastIngressFilterPriority); err != nil {
		return err
	}
	defer detachUnicastHookWarning(uplinkName, "ingress")

	if err := attachUnicastHook(uplinkName, ndpUnicastEgressProgram, "egress", unicastEgressFilterPriority); err != nil {
		return err
	}
	defer detachUnicastHookWarning(uplinkName, "egress")

	// The unicast hooks own the uplink now. A failure here leaves both hook
	// sets attached, which is safe: the unicast hooks skip work that the
	// multicast hook already did.
	if err := detachHook(uplinkName); err != nil {
		return err
	}

	stopped := make(chan struct{})
	defer close(stopped)
	go watchUnicastPeerFile(path, uplinkName, config.UplinkIPv4, information.ModTime(), stopped, verbose)

	interrupted := make(chan os.Signal, 1)
	signal.Notify(interrupted, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(interrupted)
	<-interrupted

	// Restore the multicast filters first. Both hook sets then run together
	// until the deferred unicast detachments finish, which is safe for the
	// same reason as above.
	if err := attachMulticastNeighbourHook(uplinkName); err != nil {
		fmt.Fprintf(os.Stderr, "atlas-wg-mesh: warning: restore multicast NDP: %v\n", err)
	}
	return nil
}

// installTransportNeighbours adds one permanent, externally learned,
// managed neighbour entry per peer on the discovery interface. The entry
// holds no MAC: the kernel resolves it, which the BPF FIB lookup needs.
func installTransportNeighbours(peers []netip.Addr, uplinkName string) error {
	for _, peer := range peers {
		err := runCommand("ip", "neigh", "replace", peer.String(), "dev", uplinkName, "nud", "permanent", "extern_learn", "managed")
		if err != nil {
			return fmt.Errorf("install neighbour entry for %s: %w", peer, err)
		}
	}
	return nil
}

// attachMulticastNeighbourHook restores the multicast NDP filters on the
// uplink, returning the host to multicast behaviour.
func attachMulticastNeighbourHook(uplinkName string) error {
	if err := attachHook(uplinkName, ndpProgram, "ingress"); err != nil {
		return err
	}
	return attachHook(uplinkName, ndpProgram, "egress")
}

// watchUnicastPeerFile adopts a changed peer file after a successful parse.
// An invalid file keeps the previous list until it becomes valid again.
func watchUnicastPeerFile(
	path,
	uplinkName string,
	self [4]byte,
	modified time.Time,
	stopped <-chan struct{},
	verbose bool,
) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-stopped:
			return
		case <-ticker.C:
		}

		information, err := os.Stat(path)
		if err != nil || information.ModTime().Equal(modified) {
			continue
		}

		entries, err := readUnicastPeerEntries(path)
		if err != nil {
			fmt.Fprintf(os.Stderr, "atlas-wg-mesh: warning: reload %s: %v\n", path, err)
			continue
		}

		peers := unicastTransportPeers(entries, self)

		// The neighbour entries go in first, so a new peer has its entry
		// before the map can send wrapped packets to it.
		if err := installTransportNeighbours(peers, uplinkName); err != nil {
			fmt.Fprintf(os.Stderr, "atlas-wg-mesh: warning: update transport neighbours: %v\n", err)
			continue
		}

		if err := syncUnicastPeerMap(entries, self); err != nil {
			fmt.Fprintf(os.Stderr, "atlas-wg-mesh: warning: update peer list map: %v\n", err)
			continue
		}

		modified = information.ModTime()
		logUnicastSync(verbose, peers)
	}
}

func logUnicastSync(verbose bool, peers []netip.Addr) {
	if !verbose {
		return
	}
	fmt.Printf("unicast: %d peers\n", len(peers))
}

// attachUnicastHook attaches one pinned unicast program at its own filter
// priority, separate from the multicast hooks at priority 10.
func attachUnicastHook(interfaceName, program, direction, priority string) error {
	path, err := programPath(program)
	if err != nil {
		return err
	}
	_ = runCommand("tc", "qdisc", "add", "dev", interfaceName, "clsact")
	return runCommand("tc", "filter", "replace", "dev", interfaceName, direction, "prio", priority, "handle", "1", "bpf", "direct-action", "object-pinned", path)
}

// detachUnicastHook removes one unicast filter. A missing filter is not an
// error, so a crashed daemon can be cleaned up safely.
func detachUnicastHook(interfaceName, direction, priority string) error {
	err := runCommand("tc", "filter", "del", "dev", interfaceName, direction, "prio", priority, "handle", "1", "bpf")
	if err != nil && !deleteMissing(err) {
		return err
	}
	return nil
}

func detachUnicastHookWarning(interfaceName, direction string) {
	priority := unicastEgressFilterPriority
	if direction == "ingress" {
		priority = unicastIngressFilterPriority
	}
	if err := detachUnicastHook(interfaceName, direction, priority); err != nil {
		fmt.Fprintf(os.Stderr, "atlas-wg-mesh: warning: detach unicast %s hook: %v\n", direction, err)
	}
}

// lockUnicastDaemon allows one daemon and releases automatically on a crash.
func lockUnicastDaemon() (func(), error) {
	if err := os.MkdirAll(filepath.Dir(unicastLockPath), 0755); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(unicastLockPath, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		file.Close()
		return nil, fmt.Errorf("unicast daemon is already running: %w", err)
	}
	// Make the deferred unlock safe after an early release.
	var once sync.Once
	return func() {
		once.Do(func() {
			_ = syscall.Flock(int(file.Fd()), syscall.LOCK_UN)
			_ = file.Close()
		})
	}, nil
}
