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
		if err := configureTrafficControl(ctx, request.trafficControl()); err != nil {
			return Interface{}, fmt.Errorf("configure traffic control: %w", err)
		}
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
	if egress.HasVirtualEthernet() {
		steps = append(steps, virtualEthernetSteps(namespace, hostVirtualEthernet, guestVirtualEthernet, hostIPAddress, namespaceIPAddress)...)
	}
	if egress.HasInternetPath() {
		steps = append(steps, internetPathSteps(namespace, guestVirtualEthernet, hostIPAddress)...)
	}
	if request.PublicIPv4 != "" {
		if !egress.HasInternetPath() {
			return Interface{}, fmt.Errorf("public IPv4 requires %s egress", vm.EgressUplink)
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
	if err := configureTrafficControl(ctx, request.trafficControl()); err != nil {
		releaseError := allocator.Release(ctx, request.VirtualMachineID)
		return Interface{}, errors.Join(fmt.Errorf("configure traffic control: %w", err), releaseError)
	}
	return allocator.Resolve(request.VirtualMachineID), nil
}

// Update applies mutable network settings to an allocated virtual machine. A
// failure rolls back with the reverse transition, so every step is safe to repeat.
func (allocator *LinuxAllocator) Update(ctx context.Context, request UpdateRequest) error {
	if err := allocator.update(ctx, request); err != nil {
		rollbackRequest := request
		rollbackRequest.Previous, rollbackRequest.Desired = request.Desired, request.Previous
		return errors.Join(err, allocator.update(ctx, rollbackRequest))
	}
	return nil
}

// update reconciles the host to the desired settings.
func (allocator *LinuxAllocator) update(ctx context.Context, request UpdateRequest) error {
	exists, err := networkNamespaceExists(ctx, request.VirtualMachineID)
	if err != nil || !exists {
		return err
	}

	previous, desired := request.Previous.Egress, request.Desired.Egress
	if request.Desired.PublicIPv4 != "" && !desired.HasInternetPath() {
		return fmt.Errorf("public IPv4 requires %s egress", vm.EgressUplink)
	}

	if request.Previous.PublicIPv4 != request.Desired.PublicIPv4 {
		if err := removePublicIPv4Rules(ctx, request.VirtualMachineID); err != nil {
			return err
		}
		if err := removePublicIPv4NamespaceRules(ctx, request.VirtualMachineID); err != nil {
			return err
		}
	}

	if previous.HasInternetPath() && !desired.HasInternetPath() {
		if err := removeInternetPath(ctx, request.VirtualMachineID, request.UserID); err != nil {
			return err
		}
	}
	if previous.HasVirtualEthernet() != desired.HasVirtualEthernet() {
		if err := setVirtualEthernet(ctx, request.VirtualMachineID, request.UserID, desired.HasVirtualEthernet()); err != nil {
			return err
		}
	}
	if desired.HasInternetPath() && !(previous.HasInternetPath() && previous.HasVirtualEthernet()) {
		if err := addInternetPath(ctx, request.VirtualMachineID, request.UserID); err != nil {
			return err
		}
	}

	if request.Previous.PublicIPv4 != request.Desired.PublicIPv4 && request.Desired.PublicIPv4 != "" {
		if err := addPublicIPv4(ctx, request.VirtualMachineID, request.UserID, request.Desired.PublicIPv4); err != nil {
			return err
		}
	}

	return configureTrafficControl(ctx, trafficControlRequest{
		VirtualMachineID:             request.VirtualMachineID,
		UserID:                       request.UserID,
		Egress:                       desired,
		PrivateNetworkThroughputMbps: request.Desired.PrivateNetworkThroughputMbps,
		PublicNetworkThroughputMbps:  request.Desired.PublicNetworkThroughputMbps,
	})
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
	namespaceRulesError := removePublicIPv4NamespaceRules(ctx, virtualMachineID)

	namespaceError := hostcmd.Run(ctx, "ip", "netns", "del", namespaceName(virtualMachineID))
	return errors.Join(rulesError, namespaceRulesError, namespaceError)
}

func namespaceName(virtualMachineID string) string { return "metal-" + virtualMachineID }

func namespacePath(virtualMachineID string) string {
	return "/run/netns/" + namespaceName(virtualMachineID)
}

func virtualEthernetNames(userID uint32) (host, guest string) {
	return fmt.Sprintf("vh-%d", userID), fmt.Sprintf("vg-%d", userID)
}

// virtualEthernetSteps builds the private network attachment. Only EgressNone drops it.
func virtualEthernetSteps(namespace, hostVirtualEthernet, guestVirtualEthernet, hostIPAddress, namespaceIPAddress string) [][]string {
	return [][]string{
		{"ip", "link", "add", hostVirtualEthernet, "type", "veth", "peer", "name", guestVirtualEthernet},
		{"ip", "link", "set", guestVirtualEthernet, "netns", namespace},
		{"ip", "addr", "add", hostIPAddress + "/30", "dev", hostVirtualEthernet},
		{"ip", "link", "set", hostVirtualEthernet, "up"},
		{"ip", "-n", namespace, "addr", "add", namespaceIPAddress + "/30", "dev", guestVirtualEthernet},
		{"ip", "-n", namespace, "link", "set", guestVirtualEthernet, "up"},
	}
}

// internetPathSteps builds the route out of the namespace. Only EgressUplink has it.
func internetPathSteps(namespace, guestVirtualEthernet, hostIPAddress string) [][]string {
	return append(defaultRouteSteps(namespace, hostIPAddress),
		[]string{"ip", "netns", "exec", namespace, "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", guestVirtualEthernet, "-j", "MASQUERADE"},
	)
}

// defaultRouteSteps builds the namespace route and forwarding.
func defaultRouteSteps(namespace, hostIPAddress string) [][]string {
	return [][]string{
		{"ip", "-n", namespace, "route", "replace", "default", "via", hostIPAddress},
		{"ip", "netns", "exec", namespace, "sysctl", "-q", "-w", "net.ipv4.ip_forward=1"},
	}
}

// setVirtualEthernet adds or removes the veth pair.
func setVirtualEthernet(ctx context.Context, virtualMachineID string, userID uint32, present bool) error {
	namespace := namespaceName(virtualMachineID)
	hostVirtualEthernet, guestVirtualEthernet := virtualEthernetNames(userID)
	exists, err := networkLinkExists(ctx, hostVirtualEthernet)
	if err != nil || exists == present {
		return err
	}
	if !present {
		return hostcmd.Run(ctx, "ip", "link", "del", hostVirtualEthernet)
	}

	hostIPAddress, namespaceIPAddress := transitAddresses(userID)
	return runSteps(ctx, virtualEthernetSteps(namespace, hostVirtualEthernet, guestVirtualEthernet, hostIPAddress, namespaceIPAddress))
}

// addInternetPath adds the namespace route and NAT rule.
func addInternetPath(ctx context.Context, virtualMachineID string, userID uint32) error {
	namespace := namespaceName(virtualMachineID)
	_, guestVirtualEthernet := virtualEthernetNames(userID)
	hostIPAddress, _ := transitAddresses(userID)
	if err := runSteps(ctx, defaultRouteSteps(namespace, hostIPAddress)); err != nil {
		return err
	}
	return setMasquerade(ctx, namespace, guestVirtualEthernet, true)
}

// removeInternetPath removes the NAT rule before the route.
func removeInternetPath(ctx context.Context, virtualMachineID string, userID uint32) error {
	namespace := namespaceName(virtualMachineID)
	_, guestVirtualEthernet := virtualEthernetNames(userID)
	if err := setMasquerade(ctx, namespace, guestVirtualEthernet, false); err != nil {
		return err
	}
	return removeDefaultRoute(ctx, namespace)
}

// setMasquerade adds or removes the namespace NAT rule. iptables fails on a
// duplicate add and on a delete for an absent rule, so it checks first.
func setMasquerade(ctx context.Context, namespace, guestVirtualEthernet string, present bool) error {
	prefix := namespaceCommandPrefix(namespace)
	rule := []string{"POSTROUTING", "-o", guestVirtualEthernet, "-j", "MASQUERADE"}

	check := commandWithPrefix(prefix, "iptables", append([]string{"-t", "nat", "-C"}, rule...)...)
	if exists := hostcmd.Run(ctx, check[0], check[1:]...) == nil; exists == present {
		return nil
	}

	action := "-D"
	if present {
		action = "-A"
	}
	command := commandWithPrefix(prefix, "iptables", append([]string{"-t", "nat", action}, rule...)...)
	return hostcmd.Run(ctx, command[0], command[1:]...)
}

// removeDefaultRoute removes the namespace default route when it is present.
func removeDefaultRoute(ctx context.Context, namespace string) error {
	output, err := hostcmd.Output(ctx, "ip", "-n", namespace, "route", "show", "default")
	if err != nil {
		return fmt.Errorf("show default route: %w", err)
	}
	if strings.TrimSpace(output) == "" {
		return nil
	}
	return hostcmd.Run(ctx, "ip", "-n", namespace, "route", "del", "default")
}

// networkLinkExists reports whether one host network interface is present.
func networkLinkExists(ctx context.Context, name string) (bool, error) {
	output, err := hostcmd.Output(ctx, "ip", "-o", "link", "show")
	if err != nil {
		return false, fmt.Errorf("list network links: %w", err)
	}
	for _, line := range strings.Split(output, "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		device := strings.SplitN(strings.TrimSuffix(fields[1], ":"), "@", 2)[0]
		if device == name {
			return true, nil
		}
	}
	return false, nil
}

func runSteps(ctx context.Context, steps [][]string) error {
	for _, step := range steps {
		if err := hostcmd.Run(ctx, step[0], step[1:]...); err != nil {
			return err
		}
	}
	return nil
}

func addPublicIPv4(ctx context.Context, virtualMachineID string, userID uint32, publicIPv4 string) error {
	namespace := namespaceName(virtualMachineID)
	_, guestVirtualEthernet := virtualEthernetNames(userID)
	_, namespaceIPAddress := transitAddresses(userID)
	return runSteps(ctx, publicIPv4Steps(virtualMachineID, namespace, guestVirtualEthernet, namespaceIPAddress, publicIPv4))
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
	return removePublicIPv4RulesFrom(ctx, nil, virtualMachineID)
}

func removePublicIPv4NamespaceRules(ctx context.Context, virtualMachineID string) error {
	return removePublicIPv4RulesFrom(ctx, namespaceCommandPrefix(namespaceName(virtualMachineID)), virtualMachineID)
}

// namespaceCommandPrefix runs a host command inside one VM network namespace.
func namespaceCommandPrefix(namespace string) []string {
	return []string{"ip", "netns", "exec", namespace}
}

func removePublicIPv4RulesFrom(ctx context.Context, prefix []string, virtualMachineID string) error {
	var cleanupErrors []error
	for _, table := range []string{"nat", "filter"} {
		arguments := commandWithPrefix(prefix, "iptables", "-t", table, "-S")
		output, err := hostcmd.Output(ctx, arguments[0], arguments[1:]...)
		if err != nil {
			cleanupErrors = append(cleanupErrors, fmt.Errorf("list %s rules: %w", table, err))
			continue
		}
		for _, line := range strings.Split(output, "\n") {
			ruleArguments := strings.Fields(line)
			if !hasRuleComment(ruleArguments, publicIPv4Comment(virtualMachineID)) {
				continue
			}
			if len(ruleArguments) < 2 || ruleArguments[0] != "-A" {
				continue
			}
			ruleArguments[0] = "-D"
			for index, argument := range ruleArguments {
				ruleArguments[index] = strings.Trim(argument, "\"")
			}
			arguments = commandWithPrefix(prefix, "iptables", "-t", table)
			arguments = append(arguments, ruleArguments...)
			if err := hostcmd.Run(ctx, arguments[0], arguments[1:]...); err != nil {
				cleanupErrors = append(cleanupErrors, fmt.Errorf("remove %s rule: %w", table, err))
			}
		}
	}
	return errors.Join(cleanupErrors...)
}

func commandWithPrefix(prefix []string, command string, arguments ...string) []string {
	result := append([]string(nil), prefix...)
	result = append(result, command)
	return append(result, arguments...)
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
