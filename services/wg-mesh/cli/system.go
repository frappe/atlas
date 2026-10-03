package main

import (
	"fmt"
	"net"
	"net/netip"
	"os"
	"os/exec"
	"strings"
	"syscall"
)

// The VLAN route for VM destinations. Linux runs NDP for a remote VM on the uplink.
const meshRoutePrefix = "fdaa::/16"

// readHostConfig reads the host configuration from its interfaces.
func readHostConfig(uplinkName, wireGuardName string) (hostConfig, error) {
	uplink, err := net.InterfaceByName(uplinkName)
	if err != nil {
		return hostConfig{}, err
	}

	wireGuard, err := net.InterfaceByName(wireGuardName)
	if err != nil {
		return hostConfig{}, err
	}

	public := publicInterface(uplink)
	if len(uplink.HardwareAddr) != 6 || len(public.HardwareAddr) != 6 {
		return hostConfig{}, fmt.Errorf("%s or %s has no Ethernet MAC address", uplink.Name, public.Name)
	}

	uplinkIPv4, err := interfaceAddress(uplink, "IPv4", func(address netip.Addr) bool { return address.Is4() })
	if err != nil {
		return hostConfig{}, err
	}

	// The kernel sends its neighbour solicitations from this address. Link-local is enough.
	uplinkIPv6, err := interfaceAddress(uplink, "IPv6", func(address netip.Addr) bool { return address.Is6() })
	if err != nil {
		return hostConfig{}, err
	}

	wireGuardIPv6, err := interfaceAddress(wireGuard, "global IPv6", func(address netip.Addr) bool {
		return address.Is6() && !address.IsLinkLocalUnicast()
	})
	if err != nil {
		return hostConfig{}, err
	}

	return hostConfig{
		UplinkIfIndex: uint32(uplink.Index),
		PublicIfIndex: uint32(public.Index),
		UplinkIPv4:    uplinkIPv4.As4(),
		UplinkIPv6:    uplinkIPv6.As16(),
		WireGuardIPv6: wireGuardIPv6.As16(),
		UplinkMAC:     [6]byte(uplink.HardwareAddr),
		PublicMAC:     [6]byte(public.HardwareAddr),
	}, nil
}

// publicInterface returns the interface of the IPv4 default route, or the uplink when there is none.
func publicInterface(uplink *net.Interface) *net.Interface {
	output, err := commandOutput("ip", "-4", "-o", "route", "show", "default")
	if err != nil {
		return uplink
	}

	public, err := net.InterfaceByName(fieldAfter(strings.Fields(output), "dev"))
	if err != nil {
		return uplink
	}

	return public
}

func interfaceAddress(device *net.Interface, kind string, match func(netip.Addr) bool) (netip.Addr, error) {
	addresses, err := device.Addrs()
	if err != nil {
		return netip.Addr{}, err
	}

	for _, address := range addresses {
		if prefix, err := netip.ParsePrefix(address.String()); err == nil && match(prefix.Addr().Unmap()) {
			return prefix.Addr().Unmap(), nil
		}
	}

	return netip.Addr{}, fmt.Errorf("%s has no %s address", device.Name, kind)
}

// interfaceWithAddress names the interface that holds an address, or returns "".
func interfaceWithAddress(target [16]byte) string {
	interfaces, _ := net.Interfaces()
	for _, device := range interfaces {
		if _, err := interfaceAddress(&device, "", func(address netip.Addr) bool { return address.As16() == target }); err == nil {
			return device.Name
		}
	}

	return ""
}

func deviceName(ifindex uint32) string {
	device, err := net.InterfaceByIndex(int(ifindex))
	if err != nil {
		return fmt.Sprintf("ifindex:%d", ifindex)
	}

	return device.Name
}

func parseMeshAddress(text string) ([16]byte, error) {
	address, err := netip.ParseAddr(text)
	if err != nil || !address.Is6() || !netip.MustParsePrefix(meshRoutePrefix).Contains(address) {
		return [16]byte{}, fmt.Errorf("%q is not an address in %s", text, meshRoutePrefix)
	}

	return address.As16(), nil
}

// attachHook attaches a pinned program to one direction of an interface.
func attachHook(interfaceName, program, direction string) error {
	path, err := programPath(program)
	if err != nil {
		return err
	}
	return attachProgram(interfaceName, path, direction)
}

func attachProgram(interfaceName, path, direction string) error {

	_ = runCommand("tc", "qdisc", "add", "dev", interfaceName, "clsact")

	return runCommand("tc", "filter", "replace", "dev", interfaceName, direction, "prio", "10", "handle", "1", "bpf", "direct-action", "object-pinned", path)
}

func detachHook(interfaceName, direction string) error {
	return ignoreMissing(runCommand("tc", "filter", "del", "dev", interfaceName, direction, "prio", "10", "handle", "1", "bpf"))
}

func isHookAttached(interfaceName, direction string) bool {
	output, err := commandOutput("tc", "filter", "show", "dev", interfaceName, direction)

	return err == nil && strings.Contains(output, "handle 0x1")
}

// ignoreMissing hides the error of deleting something that is already gone.
func ignoreMissing(err error) error {
	if err == nil {
		return nil
	}

	message := strings.ToLower(err.Error())
	// iproute2 reports a missing route as "No such process".
	if strings.Contains(message, "no such file or directory") || strings.Contains(message, "cannot find") || strings.Contains(message, "not found") || strings.Contains(message, "no such process") {
		return nil
	}

	return err
}

// lockFile takes an exclusive lock that the kernel releases when the process exits.
func lockFile(path string, wait bool) (func(), error) {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}

	operation := syscall.LOCK_EX
	if !wait {
		operation |= syscall.LOCK_NB
	}
	if err := syscall.Flock(int(file.Fd()), operation); err != nil {
		file.Close()
		return nil, fmt.Errorf("lock %s: %w", path, err)
	}

	return func() { file.Close() }, nil
}

func runCommand(name string, arguments ...string) error {
	_, err := commandOutput(name, arguments...)

	return err
}

func commandOutput(name string, arguments ...string) (string, error) {
	output, err := exec.Command(name, arguments...).CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("%s %s: %w: %s", name, strings.Join(arguments, " "), err, strings.TrimSpace(string(output)))
	}

	return string(output), nil
}

func fieldAfter(fields []string, name string) string {
	for index, field := range fields {
		if field == name && index+1 < len(fields) {
			return fields[index+1]
		}
	}

	return ""
}
