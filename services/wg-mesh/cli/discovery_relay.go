package main

import (
	"bufio"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/spf13/cobra"
	"golang.org/x/sys/unix"
)

const (
	defaultDiscoveryTap    = "atlas-wg-relay"
	discoveryRelayLockPath = "/run/lock/atlas-wg-mesh-discovery-relay.lock"
	discoveryFrameSize     = 14 + 20 + 8 + meshAnnouncementSize
	discoveryMessageOffset = discoveryFrameSize - meshAnnouncementSize

	// Read past a discovery frame so an oversized one fails the length check
	// rather than arriving truncated to exactly the expected size.
	discoveryReadSize = 2048
)

var discoveryVerbose bool

var discoveryRelayCommand = &cobra.Command{
	Use:   "discovery-relay PEERS_FILE",
	Short: "relay multicast discovery messages to configured hosts",
	Args:  cobra.ExactArgs(1),
	RunE: func(_ *cobra.Command, arguments []string) error {
		return runDiscoveryRelay(arguments[0], discoveryVerbose)
	},
}

// runDiscoveryRelay directs multicast-style discovery through a local relay.
func runDiscoveryRelay(peersFile string, verbose bool) error {
	config, err := readPinnedConfig()
	if err != nil {
		return err
	}
	uplinkName := interfaceWithIPv4(config.UplinkIPv4)
	if uplinkName == "" {
		return errors.New("cannot find the configured uplink")
	}
	uplink, err := net.InterfaceByName(uplinkName)
	if err != nil {
		return err
	}
	peers, err := readDiscoveryPeers(peersFile, config.UplinkIPv4)
	if err != nil {
		return err
	}
	peerFile, err := os.Stat(peersFile)
	if err != nil {
		return err
	}
	unlock, err := lockDiscoveryRelay()
	if err != nil {
		return err
	}
	defer unlock()

	var currentPeers atomic.Value
	currentPeers.Store(peers)
	stopped := make(chan struct{})
	defer close(stopped)
	go watchDiscoveryPeers(peersFile, config.UplinkIPv4, peerFile.ModTime(), &currentPeers, stopped)

	tap, tapInterface, err := createDiscoveryTap(defaultDiscoveryTap)
	if err != nil {
		return err
	}
	defer tap.Close()
	if err := setDiscoveryInterface(uint32(tapInterface.Index)); err != nil {
		return err
	}
	defer func() {
		if err := setDiscoveryInterface(uint32(uplink.Index)); err != nil {
			fmt.Fprintf(os.Stderr, "atlas-wg-mesh: warning: restore multicast discovery: %v\n", err)
		}
	}()

	// Source relayed messages from the uplink address so FOUND returns there.
	socket, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IP(config.UplinkIPv4[:]), Port: meshPort})
	if err != nil {
		return err
	}
	defer socket.Close()

	interrupted := make(chan os.Signal, 1)
	signal.Notify(interrupted, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(interrupted)
	go func() {
		<-interrupted
		_ = tap.Close()
	}()

	go relayAnnouncements(socket, config.UplinkIPv4, &currentPeers, verbose)
	return relayTapFrames(tap, socket, &currentPeers, verbose)
}

// watchDiscoveryPeers replaces the peer list after a successful file update.
func watchDiscoveryPeers(path string, self [4]byte, modified time.Time, current *atomic.Value, stopped <-chan struct{}) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-stopped:
			return
		case <-ticker.C:
		}

		info, err := os.Stat(path)
		if err != nil || info.ModTime().Equal(modified) {
			continue
		}
		peers, err := readDiscoveryPeers(path, self)
		if err != nil {
			fmt.Fprintf(os.Stderr, "atlas-wg-mesh: warning: reload %s: %v\n", path, err)
			continue
		}

		current.Store(peers)
		modified = info.ModTime()
	}
}

// readDiscoveryPeers parses one IPv4 address per non-comment line. It drops
// this host, which would otherwise relay its own NOW_HERE back to itself.
func readDiscoveryPeers(path string, self [4]byte) ([]*net.UDPAddr, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	peers := make([]*net.UDPAddr, 0)
	scanner := bufio.NewScanner(file)
	line := 0
	for scanner.Scan() {
		line++
		value := strings.TrimSpace(strings.SplitN(scanner.Text(), "#", 2)[0])
		if value == "" {
			continue
		}
		address := net.ParseIP(value)
		if address == nil || address.To4() == nil {
			return nil, fmt.Errorf("%s:%d: expected an IPv4 address", path, line)
		}
		if [4]byte(address.To4()) == self {
			continue
		}
		peers = append(peers, &net.UDPAddr{IP: address, Port: meshPort})
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	if len(peers) == 0 {
		return nil, fmt.Errorf("%s contains no peers", path)
	}
	return peers, nil
}

// createDiscoveryTap creates and enables the TAP used for BPF redirects.
//
// The descriptor is opened raw rather than with os.OpenFile, which would hand
// it to the runtime poller before TUNSETIFF attaches a device. Polling an
// unattached tun latches EPOLLERR, and the first read then fails with "not
// pollable". Registering after the ioctl keeps Close able to stop a read.
func createDiscoveryTap(name string) (tap *os.File, iface *net.Interface, err error) {
	descriptor, err := unix.Open("/dev/net/tun", unix.O_RDWR, 0)
	if err != nil {
		return nil, nil, err
	}
	defer func() {
		if err != nil {
			unix.Close(descriptor)
		}
	}()

	request, err := unix.NewIfreq(name)
	if err != nil {
		return nil, nil, err
	}
	request.SetUint16(unix.IFF_TAP | unix.IFF_NO_PI)
	if err = unix.IoctlIfreq(descriptor, unix.TUNSETIFF, request); err != nil {
		return nil, nil, err
	}
	if err = unix.SetNonblock(descriptor, true); err != nil {
		return nil, nil, err
	}
	tap = os.NewFile(uintptr(descriptor), "/dev/net/tun")
	// Silence the kernel's own IPv6 traffic on a device that carries only
	// discovery frames.
	_ = runCommand("sysctl", "-qw", "net.ipv6.conf."+request.Name()+".disable_ipv6=1")
	if err = runCommand("ip", "link", "set", request.Name(), "up"); err != nil {
		return nil, nil, err
	}
	iface, err = net.InterfaceByName(request.Name())
	if err != nil {
		return nil, nil, err
	}
	return tap, iface, nil
}

// relayTapFrames forwards WHO_HAS frames emitted by BPF.
func relayTapFrames(tap *os.File, socket *net.UDPConn, currentPeers *atomic.Value, verbose bool) error {
	frame := make([]byte, discoveryReadSize)
	for {
		length, err := tap.Read(frame)
		if err != nil {
			if errors.Is(err, os.ErrClosed) {
				return nil
			}
			return err
		}

		message, ok := whoHasMessage(frame[:length])
		if !ok {
			continue
		}
		logRelayMessage(verbose, "RX", message, nil)
		relayMessage(socket, currentPeers, message, verbose)
	}
}

// relayAnnouncements forwards local NOW_HERE announcements from the CLI.
//
// Only this host may be relayed. The uplink hook normally consumes discovery
// from other hosts before it reaches any socket, but while that hook is
// detached those frames arrive here instead, and forwarding them would make
// this host reflect every announcement it receives.
func relayAnnouncements(socket *net.UDPConn, self [4]byte, currentPeers *atomic.Value, verbose bool) {
	message := make([]byte, meshAnnouncementSize)
	for {
		length, from, err := socket.ReadFromUDP(message)
		if err != nil {
			return
		}

		if !from.IP.Equal(net.IP(self[:])) {
			continue
		}
		if length != len(message) || message[0] != 1 || message[1] != 4 {
			continue
		}
		logRelayMessage(verbose, "RX", message, nil)
		relayMessage(socket, currentPeers, message, verbose)
	}
}

// relayMessage sends one discovery payload to every configured peer. An
// unreachable peer is reported and skipped; the rest still get the message.
func relayMessage(socket *net.UDPConn, currentPeers *atomic.Value, message []byte, verbose bool) {
	for _, peer := range currentPeers.Load().([]*net.UDPAddr) {
		if _, err := socket.WriteToUDP(message, peer); err != nil {
			fmt.Fprintf(os.Stderr, "atlas-wg-mesh: warning: relay to %s: %v\n", peer, err)
			continue
		}
		logRelayMessage(verbose, "TX", message, peer)
	}
}

// whoHasMessage returns the payload from a BPF-generated WHO_HAS frame.
func whoHasMessage(frame []byte) ([]byte, bool) {
	if len(frame) != discoveryFrameSize {
		return nil, false
	}
	message := frame[discoveryMessageOffset:]
	return message, message[0] == 1 && message[1] == 1
}

// logRelayMessage prints accepted and forwarded discovery messages on request.
func logRelayMessage(verbose bool, direction string, message []byte, peer *net.UDPAddr) {
	if !verbose {
		return
	}
	operation := operationName(message[1])
	virtualMachine := netip.AddrFrom16([16]byte(message[4:20]))
	if peer == nil {
		fmt.Printf("%s %s vm=%s\n", direction, operation, virtualMachine)
		return
	}
	fmt.Printf("%s %s vm=%s peer=%s\n", direction, operation, virtualMachine, peer)
}

// lockDiscoveryRelay allows one relay and releases automatically on a crash.
func lockDiscoveryRelay() (func(), error) {
	if err := os.MkdirAll(filepath.Dir(discoveryRelayLockPath), 0755); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(discoveryRelayLockPath, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		file.Close()
		return nil, fmt.Errorf("discovery relay is already running: %w", err)
	}
	return func() {
		_ = syscall.Flock(int(file.Fd()), syscall.LOCK_UN)
		_ = file.Close()
	}, nil
}
