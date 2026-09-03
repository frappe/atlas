package network

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"strings"

	"github.com/frappe/atlas/metal/internal/hostcmd"
	"github.com/frappe/atlas/metal/internal/vm"
)

const (
	tapName             = "tap0"
	gatewayIPAddress    = "172.16.0.1"
	guestIPAddress      = "172.16.0.2"
	networkPrefixLength = 24
)

// LinuxAllocator creates Linux network resources for virtual machines.
type LinuxAllocator struct{}

// NewLinuxAllocator returns a Linux network allocator.
func NewLinuxAllocator() *LinuxAllocator { return &LinuxAllocator{} }

// Allocate creates virtual machine network resources.
func (allocator *LinuxAllocator) Allocate(ctx context.Context, request Request) (Interface, error) {
	exists, err := networkNamespaceExists(ctx, request.VirtualMachineID)
	if err != nil {
		return Interface{}, err
	}
	if exists {
		return allocator.Resolve(request.VirtualMachineID), nil
	}

	namespace := namespaceName(request.VirtualMachineID)
	userID, groupID := fmt.Sprint(request.UserID), fmt.Sprint(request.GroupID)
	hostVirtualEthernet, guestVirtualEthernet := virtualEthernetNames(request.UserID)
	hostIPAddress, namespaceIPAddress := transitAddresses(request.UserID)
	gatewayCIDR := fmt.Sprintf("%s/%d", gatewayIPAddress, networkPrefixLength)

	steps := [][]string{
		{"ip", "netns", "add", namespace},
		{"ip", "-n", namespace, "link", "set", "lo", "up"},

		{"ip", "-n", namespace, "tuntap", "add", tapName, "mode", "tap", "user", userID, "group", groupID},
		{"ip", "-n", namespace, "addr", "add", gatewayCIDR, "dev", tapName},
		{"ip", "-n", namespace, "link", "set", tapName, "up"},
	}
	egress := request.Egress
	if egress == "" {
		egress = vm.EgressHost
	}
	if egress == vm.EgressHost {
		steps = append(steps,
			[]string{"ip", "link", "add", hostVirtualEthernet, "type", "veth", "peer", "name", guestVirtualEthernet},
			[]string{"ip", "link", "set", guestVirtualEthernet, "netns", namespace},
			[]string{"ip", "addr", "add", hostIPAddress + "/30", "dev", hostVirtualEthernet},
			[]string{"ip", "link", "set", hostVirtualEthernet, "up"},
			[]string{"ip", "-n", namespace, "addr", "add", namespaceIPAddress + "/30", "dev", guestVirtualEthernet},
			[]string{"ip", "-n", namespace, "link", "set", guestVirtualEthernet, "up"},
			[]string{"ip", "-n", namespace, "route", "add", "default", "via", hostIPAddress},
			[]string{"ip", "netns", "exec", namespace, "sysctl", "-q", "-w", "net.ipv4.ip_forward=1"},
			[]string{"ip", "netns", "exec", namespace, "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", guestVirtualEthernet, "-j", "MASQUERADE"},
		)
	}
	if request.PublicIPv4 != "" {
		if egress != vm.EgressHost {
			return Interface{}, fmt.Errorf("public IPv4 requires host egress")
		}
		steps = append(steps, publicIPv4Steps(request.VirtualMachineID, namespace, guestVirtualEthernet, namespaceIPAddress, request.PublicIPv4)...)
	}
	for _, step := range steps {
		if err := hostcmd.Run(ctx, step[0], step[1:]...); err != nil {
			releaseError := allocator.Release(ctx, request.VirtualMachineID)
			linkError := hostcmd.Run(ctx, "ip", "link", "del", hostVirtualEthernet)
			return Interface{}, errors.Join(err, releaseError, linkError)
		}
	}
	return allocator.Resolve(request.VirtualMachineID), nil
}

// Resolve returns network settings for one virtual machine.
func (allocator *LinuxAllocator) Resolve(virtualMachineID string) Interface {
	return Interface{
		NetworkNamespacePath: namespacePath(virtualMachineID),
		TapName:              tapName,
		MACAddress:           macAddressFor(virtualMachineID),
		GuestIPAddress:       guestIPAddress,
		GatewayIPAddress:     gatewayIPAddress,
	}
}

// Release removes a virtual machine network.
func (allocator *LinuxAllocator) Release(ctx context.Context, virtualMachineID string) error {
	rulesError := removePublicIPv4Rules(ctx, virtualMachineID)

	exists, err := networkNamespaceExists(ctx, virtualMachineID)
	if err != nil {
		return errors.Join(rulesError, err)
	}
	if !exists {
		return rulesError
	}

	namespaceError := hostcmd.Run(ctx, "ip", "netns", "del", namespaceName(virtualMachineID))
	return errors.Join(rulesError, namespaceError)
}

func namespaceName(virtualMachineID string) string { return "metal-" + virtualMachineID }

func namespacePath(virtualMachineID string) string {
	return "/run/netns/" + namespaceName(virtualMachineID)
}

func virtualEthernetNames(userID uint32) (host, guest string) {
	return fmt.Sprintf("vh-%d", userID), fmt.Sprintf("vg-%d", userID)
}

func transitAddresses(userID uint32) (hostIPAddress, namespaceIPAddress string) {
	networkAddress := uint32(0x0A000000) | ((userID & 0x3FFFFF) << 2)
	return addressString(networkAddress + 1), addressString(networkAddress + 2)
}

func addressString(value uint32) string {
	return fmt.Sprintf("%d.%d.%d.%d", byte(value>>24), byte(value>>16), byte(value>>8), byte(value))
}

func networkNamespaceExists(ctx context.Context, virtualMachineID string) (bool, error) {
	output, err := hostcmd.Output(ctx, "ip", "netns", "list")
	if err != nil {
		return false, fmt.Errorf("list network namespaces: %w", err)
	}

	name := namespaceName(virtualMachineID)
	for _, line := range strings.Split(output, "\n") {
		fields := strings.Fields(line)
		if len(fields) > 0 && fields[0] == name {
			return true, nil
		}
	}
	return false, nil
}

func publicIPv4Steps(virtualMachineID, namespace, guestVirtualEthernet, namespaceIPAddress, publicIPv4 string) [][]string {
	comment := publicIPv4Comment(virtualMachineID)
	return [][]string{
		{"iptables", "-t", "nat", "-A", "PREROUTING", "-d", publicIPv4, "-m", "comment", "--comment", comment, "-j", "DNAT", "--to-destination", namespaceIPAddress},
		{"iptables", "-t", "nat", "-I", "POSTROUTING", "1", "-s", namespaceIPAddress, "-m", "comment", "--comment", comment, "-j", "SNAT", "--to-source", publicIPv4},
		{"iptables", "-A", "FORWARD", "-d", namespaceIPAddress, "-m", "conntrack", "--ctstate", "NEW,ESTABLISHED,RELATED", "-m", "comment", "--comment", comment, "-j", "ACCEPT"},
		{"iptables", "-A", "FORWARD", "-s", namespaceIPAddress, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-m", "comment", "--comment", comment, "-j", "ACCEPT"},
		{"ip", "netns", "exec", namespace, "iptables", "-t", "nat", "-A", "PREROUTING", "-i", guestVirtualEthernet, "-d", namespaceIPAddress, "-m", "comment", "--comment", comment, "-j", "DNAT", "--to-destination", guestIPAddress},
	}
}

func removePublicIPv4Rules(ctx context.Context, virtualMachineID string) error {
	var cleanupErrors []error
	for _, table := range []string{"nat", "filter"} {
		output, err := hostcmd.Output(ctx, "iptables", "-t", table, "-S")
		if err != nil {
			cleanupErrors = append(cleanupErrors, fmt.Errorf("list %s rules: %w", table, err))
			continue
		}
		for _, line := range strings.Split(output, "\n") {
			arguments := strings.Fields(line)
			if !hasRuleComment(arguments, publicIPv4Comment(virtualMachineID)) {
				continue
			}
			if len(arguments) < 2 || arguments[0] != "-A" {
				continue
			}
			arguments[0] = "-D"
			for index, argument := range arguments {
				arguments[index] = strings.Trim(argument, "\"")
			}
			if err := hostcmd.Run(ctx, "iptables", append([]string{"-t", table}, arguments...)...); err != nil {
				cleanupErrors = append(cleanupErrors, fmt.Errorf("remove %s rule: %w", table, err))
			}
		}
	}
	return errors.Join(cleanupErrors...)
}

func hasRuleComment(arguments []string, expected string) bool {
	for index, argument := range arguments {
		if argument == "--comment" && index+1 < len(arguments) {
			return strings.Trim(arguments[index+1], "\"") == expected
		}
	}
	return false
}

func publicIPv4Comment(virtualMachineID string) string {
	return "metal-public-ipv4-" + virtualMachineID
}

func macAddressFor(virtualMachineID string) string {
	digest := sha256.Sum256([]byte(virtualMachineID))
	return fmt.Sprintf("02:%02x:%02x:%02x:%02x:%02x", digest[0], digest[1], digest[2], digest[3], digest[4])
}

var _ Allocator = (*LinuxAllocator)(nil)
