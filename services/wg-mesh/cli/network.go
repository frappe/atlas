package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"
)

const (
	meshPort             = 7373
	meshMulticastAddress = "239.1.1.1"
	meshAnnouncementSize = 36

	// Multicast is unreliable, so repeat announcements to replace stale caches.
	// RFC 5227 uses two ARP announcements and QEMU sends five after migration;
	// both space them because back-to-back packets can be lost together. Three
	// give the multicast announcement enough redundancy.
	meshAnnouncementAttempts = 3
	meshAnnouncementInterval = 50 * time.Millisecond

	// The shared VLAN route. Route lookup for a VM destination selects the
	// VLAN interface, so Linux runs NDP on that link.
	meshRoutePrefix = "fdaa::/16"
)

func readHostConfig(uplinkName, wireGuardName string) (hostConfig, error) {
	uplinkInterface, err := net.InterfaceByName(uplinkName)
	if err != nil {
		return hostConfig{}, err
	}

	wireGuardInterface, err := net.InterfaceByName(wireGuardName)
	if err != nil {
		return hostConfig{}, err
	}

	uplinkIPv4, err := interfaceAddress(
		uplinkInterface,
		"IPv4",
		func(ip net.IP) bool {
			return ip.To4() != nil
		},
	)
	if err != nil {
		return hostConfig{}, err
	}

	wireGuardIPv6, err := interfaceAddress(
		wireGuardInterface,
		"global IPv6",
		func(ip net.IP) bool {
			return ip.To4() == nil &&
				!ip.IsLinkLocalUnicast()
		},
	)
	if err != nil {
		return hostConfig{}, err
	}

	/*
	 * The discovery packet is transmitted through the uplink/VLAN
	 * interface, so the BPF program needs that interface's MAC address
	 * when constructing the Ethernet header and Source Link-Layer
	 * Address option of the multicast Neighbor Solicitation.
	 */
	if len(uplinkInterface.HardwareAddr) != 6 {
		return hostConfig{}, fmt.Errorf("%s has no valid Ethernet MAC address", uplinkInterface.Name)
	}

	var discoveryMAC [6]byte
	copy(discoveryMAC[:], uplinkInterface.HardwareAddr)

	return hostConfig{
		DiscoveryIndex: uint32(uplinkInterface.Index),
		UplinkIPv4:     [4]byte(uplinkIPv4.To4()),
		WireGuardIPv6:  [16]byte(wireGuardIPv6.To16()),
		DiscoveryMAC:   discoveryMAC,
	}, nil
}

type hostInterfaces struct {
	uplinkName    string
	wireGuardName string
}

func findNetworkInterface(name string) (net.Interface, bool, error) {
	interfaces, err := net.Interfaces()
	if err != nil {
		return net.Interface{}, false, err
	}

	for _, networkInterface := range interfaces {
		if networkInterface.Name == name {
			return networkInterface, true, nil
		}
	}

	return net.Interface{}, false, nil
}

func configuredInterfaces() (hostInterfaces, error) {
	config, err := readPinnedConfig()
	if err != nil {
		return hostInterfaces{}, err
	}

	interfaces := hostInterfaces{}

	interfaces.uplinkName = interfaceWithIPv4(config.UplinkIPv4)

	if wireGuardName := interfaceWithAddress(config.WireGuardIPv6); wireGuardName != "" {
		interfaces.wireGuardName = wireGuardName
	}

	return interfaces, nil
}

func interfaceWithIPv4(target [4]byte) string {
	interfaces, err := net.Interfaces()
	if err != nil {
		return ""
	}

	for _, iface := range interfaces {
		addresses, err := iface.Addrs()
		if err != nil {
			continue
		}

		for _, address := range addresses {
			network, ok := address.(*net.IPNet)
			if ok &&
				network.IP.To4() != nil &&
				[4]byte(network.IP.To4()) == target {
				return iface.Name
			}
		}
	}

	return ""
}

func interfaceWithAddress(target [16]byte) string {
	interfaces, err := net.Interfaces()
	if err != nil {
		return ""
	}

	for _, iface := range interfaces {
		addresses, err := iface.Addrs()
		if err != nil {
			continue
		}

		for _, address := range addresses {
			network, ok := address.(*net.IPNet)
			if !ok {
				continue
			}

			if ip := network.IP.To16(); ip != nil &&
				[16]byte(ip) == target {
				return iface.Name
			}
		}
	}

	return ""
}

func interfaceAddress(iface *net.Interface, kind string, match func(net.IP) bool) (net.IP, error) {
	addresses, err := iface.Addrs()
	if err != nil {
		return nil, err
	}

	for _, address := range addresses {
		network, ok := address.(*net.IPNet)
		if ok && match(network.IP) {
			return network.IP, nil
		}
	}

	return nil, fmt.Errorf("%s has no %s address", iface.Name, kind)
}

func parseMeshAddress(addressText string) ([16]byte, error) {
	address, err := netip.ParseAddr(addressText)
	if err != nil || !address.Is6() {
		return [16]byte{}, fmt.Errorf("%q is not an IPv6 address", addressText)
	}

	meshAddress := address.As16()

	if meshAddress[0] != 0xfd ||
		meshAddress[1] != 0xaa {
		return [16]byte{}, fmt.Errorf("%q is not in fdaa::/16", addressText)
	}

	return meshAddress, nil
}

func meshTenant(address [16]byte) uint32 {
	return binary.BigEndian.Uint32(address[4:8])
}

func announceVirtualMachine(address [16]byte, config hostConfig) error {
	destination := &net.UDPAddr{
		IP:   net.ParseIP(meshMulticastAddress),
		Port: meshPort,
	}

	conn, err := net.ListenUDP("udp4", nil)
	if err != nil {
		return err
	}
	defer conn.Close()

	if err := configureMulticastSocket(conn, config.UplinkIPv4); err != nil {
		return err
	}

	message := make([]byte, meshAnnouncementSize)

	message[0] = 1
	message[1] = 4

	copy(message[4:20], address[:])

	copy(message[20:], config.WireGuardIPv6[:])

	for attempt := range meshAnnouncementAttempts {
		if attempt > 0 {
			time.Sleep(meshAnnouncementInterval)
		}

		if _, err := conn.WriteToUDP(message, destination); err != nil {
			return err
		}
	}

	return nil
}

func configureMulticastSocket(conn *net.UDPConn, source [4]byte) error {
	rawConn, err := conn.SyscallConn()
	if err != nil {
		return err
	}

	var socketError error

	err = rawConn.Control(func(fileDescriptor uintptr) {
		descriptor := int(fileDescriptor)

		if err := syscall.SetsockoptInt(descriptor, syscall.IPPROTO_IP, syscall.IP_MULTICAST_TTL, 1); err != nil {
			socketError = err
			return
		}

		socketError = syscall.SetsockoptInet4Addr(descriptor, syscall.IPPROTO_IP, syscall.IP_MULTICAST_IF, source)
	})

	if err != nil {
		return err
	}

	return socketError
}

func mountBPFFileSystem() error {
	if err := runCommand("mountpoint", "-q", "/sys/fs/bpf"); err == nil {
		return nil
	}

	return runCommand("mount", "-t", "bpf", "bpf", "/sys/fs/bpf")
}

// attachHook attaches one pinned program to a TC direction of an interface.
// The direction is "ingress" or "egress".
func attachHook(interfaceName, program, direction string) error {
	path, err := programPath(program)
	if err != nil {
		return err
	}

	return attachHookPath(interfaceName, path, direction)
}

func attachHookPath(interfaceName, programPath, direction string) error {
	_ = runCommand("tc", "qdisc", "add", "dev", interfaceName, "clsact")

	return runCommand("tc", "filter", "replace", "dev", interfaceName, direction, "prio", "10", "handle", "1", "bpf", "direct-action", "object-pinned", programPath)
}

// detachHook removes the TC filters of both directions from an interface. A
// missing filter or qdisc is not an error.
func detachHook(interfaceName string) error {
	for _, direction := range []string{
		"ingress",
		"egress",
	} {
		err := runCommand("tc", "filter", "del", "dev", interfaceName, direction, "prio", "10", "handle", "1", "bpf")

		if err != nil && !deleteMissing(err) {
			return err
		}
	}

	return nil
}

// deleteMissing reports whether a delete error only means that the entry was
// already absent.
func deleteMissing(err error) bool {
	message := strings.ToLower(err.Error())

	return strings.Contains(message, "no such file or directory") ||
		strings.Contains(message, "cannot find")
}

// setProxyNeighbour makes the host answer neighbour solicitations for a VM
// address on the shared VLAN interface.
func setProxyNeighbour(addressText, uplinkName string) error {
	return runCommand("ip", "-6", "neigh", "replace", "proxy", addressText, "dev", uplinkName)
}

// removeProxyNeighbour stops the host from answering for a VM address. An
// absent entry is not an error.
func removeProxyNeighbour(addressText, uplinkName string) error {
	err := runCommand("ip", "-6", "neigh", "del", "proxy", addressText, "dev", uplinkName)

	if err != nil && !deleteMissing(err) {
		return err
	}

	return nil
}

// setMeshRoute points VM destinations at the shared VLAN interface, so Linux
// runs neighbour discovery for them on that link. It is not a WireGuard route.
func setMeshRoute(uplinkName string) error {
	return runCommand("ip", "-6", "route", "replace", meshRoutePrefix, "dev", uplinkName)
}

func removeMeshRoute(uplinkName string) error {
	err := runCommand("ip", "-6", "route", "del", meshRoutePrefix, "dev", uplinkName)

	if err != nil && !deleteMissing(err) {
		return err
	}

	return nil
}

// requireNeighbourKfunc rejects configuration when the Atlas kernel module is
// not loaded. The NDP hook cannot load without its kfunc.
func requireNeighbourKfunc() error {
	symbols, err := os.ReadFile("/proc/kallsyms")
	if err != nil {
		return fmt.Errorf("read /proc/kallsyms: %w", err)
	}

	if bytes.Contains(symbols, []byte("atlas_register_neigh")) {
		return nil
	}

	return errors.New("the atlas_neigh kernel module is not loaded; build it with make module and load it before configure")
}

func runCommand(name string, arguments ...string) error {
	_, err := commandOutput(name, arguments...)

	return err
}

func commandOutput(name string, arguments ...string) (string, error) {
	command := exec.Command(name, arguments...)

	output, err := command.CombinedOutput()
	if err != nil {
		return "",
			fmt.Errorf("%s %s: %w: %s", name, strings.Join(arguments, " "), err, strings.TrimSpace(string(output)))
	}

	return string(output), nil
}
