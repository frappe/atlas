package main

import (
	"fmt"
	"net"
	"net/netip"
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
	// give Atlas enough redundancy: a miss costs one WHO_HAS, not reachability.
	meshAnnouncementAttempts = 3
	meshAnnouncementInterval = 50 * time.Millisecond
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
	if len(uplinkInterface.HardwareAddr) != 6 {
		return hostConfig{}, fmt.Errorf("%s has no MAC address", uplinkName)
	}

	uplinkIPv4, err := interfaceAddress(uplinkInterface, "IPv4", func(ip net.IP) bool { return ip.To4() != nil })
	if err != nil {
		return hostConfig{}, err
	}
	wireGuardIPv6, err := interfaceAddress(wireGuardInterface, "global IPv6", func(ip net.IP) bool {
		return ip.To4() == nil && !ip.IsLinkLocalUnicast()
	})
	if err != nil {
		return hostConfig{}, err
	}

	config := hostConfig{
		UplinkIndex:   uint32(uplinkInterface.Index),
		UplinkIPv4:    [4]byte(uplinkIPv4.To4()),
		WireGuardIPv6: [16]byte(wireGuardIPv6.To16()),
	}
	copy(config.UplinkMAC[:], uplinkInterface.HardwareAddr)
	return config, nil
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
	if uplink, err := net.InterfaceByIndex(int(config.UplinkIndex)); err == nil {
		interfaces.uplinkName = uplink.Name
	}
	if wireGuardName := interfaceWithAddress(config.WireGuardIPv6); wireGuardName != "" {
		interfaces.wireGuardName = wireGuardName
	}
	return interfaces, nil
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
			if ip := network.IP.To16(); ip != nil && [16]byte(ip) == target {
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
	if meshAddress[0] != 0xfd || meshAddress[1] != 0xaa {
		return [16]byte{}, fmt.Errorf("%q is not in fdaa::/16", addressText)
	}
	return meshAddress, nil
}

func announceVirtualMachine(address [16]byte, config hostConfig) error {
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

	destination := &net.UDPAddr{IP: net.ParseIP(meshMulticastAddress), Port: meshPort}
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

func attachHook(interfaceName, program string) error {
	path, err := programPath(program)
	if err != nil {
		return err
	}
	return attachHookPath(interfaceName, path)
}

func attachHookPath(interfaceName, programPath string) error {
	_ = runCommand("tc", "qdisc", "add", "dev", interfaceName, "clsact")
	return runCommand("tc", "filter", "replace", "dev", interfaceName, "ingress", "prio", "10", "handle", "1", "bpf", "direct-action", "object-pinned", programPath)
}

func detachHook(interfaceName string) error {
	err := runCommand("tc", "filter", "del", "dev", interfaceName, "ingress", "prio", "10", "handle", "1", "bpf")
	if err != nil && strings.Contains(err.Error(), "No such file or directory") {
		return nil
	}
	return err
}

func runCommand(name string, arguments ...string) error {
	_, err := commandOutput(name, arguments...)
	return err
}

func commandOutput(name string, arguments ...string) (string, error) {
	command := exec.Command(name, arguments...)
	output, err := command.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("%s %s: %w: %s", name, strings.Join(arguments, " "), err, strings.TrimSpace(string(output)))
	}
	return string(output), nil
}
