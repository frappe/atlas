package main

import (
	"errors"
	"fmt"
	"net/netip"
	"strings"

	"github.com/cilium/ebpf"
)

func inspectVirtualMachine(addressText string) error {
	virtualMachine, err := parseMeshAddress(addressText)
	if err != nil {
		return err
	}
	local, err := isLocalVirtualMachine(virtualMachine)
	if err != nil {
		return err
	}
	fmt.Printf("VM: %s\nlocal: %t\n", addressText, local)
	if local {
		return nil
	}
	host, found, err := remoteVirtualMachineHost(virtualMachine)
	if err != nil {
		return err
	}
	if !found {
		fmt.Println("remote: not learned")
		return nil
	}
	hostAddress := netip.AddrFrom16(host)
	fmt.Printf("remote host: %s\n", hostAddress)
	return inspectWireGuardHost(hostAddress)
}

func isLocalVirtualMachine(virtualMachine [16]byte) (bool, error) {
	localVMs, err := openMap("local_vms")
	if err != nil {
		return false, err
	}
	defer localVMs.Close()

	var value uint32
	err = localVMs.Lookup(virtualMachine, &value)
	if errors.Is(err, ebpf.ErrKeyNotExist) {
		return false, nil
	}
	return err == nil, err
}

func remoteVirtualMachineHost(virtualMachine [16]byte) ([16]byte, bool, error) {
	remoteVMs, err := openMap("remote_vms")
	if err != nil {
		return [16]byte{}, false, err
	}
	defer remoteVMs.Close()

	var host [16]byte
	err = remoteVMs.Lookup(virtualMachine, &host)
	if errors.Is(err, ebpf.ErrKeyNotExist) {
		return [16]byte{}, false, nil
	}
	return host, err == nil, err
}

func inspectWireGuardHost(host netip.Addr) error {
	route, err := commandOutput("ip", "-6", "route", "get", host.String())
	if err != nil {
		return err
	}
	interfaceName := fieldAfter(strings.Fields(route), "dev")
	if interfaceName == "" {
		fmt.Printf("route: %s", route)
		return nil
	}
	fmt.Printf("route: %s", strings.TrimSpace(route))
	peer, err := wireGuardPeer(interfaceName, host)
	if err != nil {
		fmt.Printf("\nWireGuard peer: unavailable (%v)\n", err)
		return nil
	}
	if peer == "" {
		fmt.Println("\nWireGuard peer: no matching AllowedIPs entry")
		return nil
	}
	fmt.Printf("\nWireGuard peer: %s\n", peer)
	return nil
}

func wireGuardPeer(interfaceName string, host netip.Addr) (string, error) {
	output, err := commandOutput("wg", "show", interfaceName, "dump")
	if err != nil {
		return "", err
	}
	for lineNumber, line := range strings.Split(strings.TrimSpace(output), "\n") {
		if lineNumber == 0 {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) >= 5 && allowedByPeer(fields[3], host) {
			return fmt.Sprintf("public key=%s endpoint=%s handshake=%s", fields[0], fields[2], fields[4]), nil
		}
	}
	return "", nil
}

func allowedByPeer(allowedIPs string, host netip.Addr) bool {
	for _, allowedIP := range strings.Split(allowedIPs, ",") {
		prefix, err := netip.ParsePrefix(allowedIP)
		if err == nil && prefix.Contains(host) {
			return true
		}
	}
	return false
}

func fieldAfter(fields []string, fieldName string) string {
	for index, field := range fields {
		if field == fieldName && index+1 < len(fields) {
			return fields[index+1]
		}
	}
	return ""
}
