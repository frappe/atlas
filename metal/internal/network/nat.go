package network

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"

	platform "github.com/frappe/atlas/metal/internal/platform"
)

// setMasquerade adds or removes the namespace NAT rule. iptables fails on a
// duplicate add and on a delete for an absent rule, so it checks first.
func setMasquerade(ctx context.Context, namespace, guestVirtualEthernet string, present bool) error {
	prefix := namespaceCommandPrefix(namespace)
	rule := []string{"POSTROUTING", "-o", guestVirtualEthernet, "-j", "MASQUERADE"}

	check := commandWithPrefix(prefix, "iptables", append([]string{"-t", "nat", "-C"}, rule...)...)
	exists, err := ruleExists(ctx, check)
	if err != nil || exists == present {
		return err
	}

	action := "-D"
	if present {
		action = "-A"
	}
	command := commandWithPrefix(prefix, "iptables", append([]string{"-t", "nat", action}, rule...)...)
	return platform.Run(ctx, command[0], command[1:]...)
}

// maximumSegmentSizeRule clamps TCP MSS for SYN packets to the guest.
func maximumSegmentSizeRule(guestVirtualEthernet string) []string {
	return []string{
		"FORWARD", "-o", guestVirtualEthernet,
		"-p", "tcp", "--tcp-flags", "SYN,RST", "SYN",
		"-j", "TCPMSS", "--clamp-mss-to-pmtu",
	}
}

// ensureMaximumSegmentSizeClamp protects TCP from a guest MTU larger than the veth MTU.
func ensureMaximumSegmentSizeClamp(ctx context.Context, virtualMachineID string, userID uint32) error {
	prefix := namespaceCommandPrefix(namespaceName(virtualMachineID))
	_, guestVirtualEthernet := virtualEthernetNames(userID)
	rule := maximumSegmentSizeRule(guestVirtualEthernet)

	check := commandWithPrefix(prefix, "iptables", append([]string{"-t", "mangle", "-C"}, rule...)...)
	exists, err := ruleExists(ctx, check)
	if err != nil || exists {
		return err
	}

	command := commandWithPrefix(prefix, "iptables", append([]string{"-t", "mangle", "-A"}, rule...)...)
	return platform.Run(ctx, command[0], command[1:]...)
}

// ruleExists runs an iptables check. Exit code 1 means absent. Any other failure
// is an error, so a broken check does not read as absent.
func ruleExists(ctx context.Context, check []string) (bool, error) {
	err := platform.Run(ctx, check[0], check[1:]...)
	if err == nil {
		return true, nil
	}

	var exitError *exec.ExitError
	if errors.As(err, &exitError) && exitError.ExitCode() == 1 {
		return false, nil
	}
	return false, err
}

func ensurePublicIPv4(ctx context.Context, virtualMachineID string, userID uint32, publicIPv4 string) error {
	namespace := namespaceName(virtualMachineID)
	_, guestVirtualEthernet := virtualEthernetNames(userID)
	_, namespaceIPAddress := transitAddresses(userID)
	steps := publicIPv4Steps(virtualMachineID, namespace, guestVirtualEthernet, namespaceIPAddress, publicIPv4)
	return ensureRuleSet(ctx, steps, func() error {
		return errors.Join(removePublicIPv4Rules(ctx, virtualMachineID), removePublicIPv4NamespaceRules(ctx, virtualMachineID))
	})
}

// defaultRouteInterface returns the IPv4 default route device. WG Mesh uses the same public interface.
func defaultRouteInterface() (string, error) {
	contents, err := os.ReadFile("/proc/net/route")
	if err != nil {
		return "", fmt.Errorf("read the IPv4 routes: %w", err)
	}
	for _, line := range strings.Split(string(contents), "\n") {
		fields := strings.Fields(line)
		if len(fields) > 1 && fields[1] == "00000000" {
			return fields[0], nil
		}
	}
	return "", errors.New("the host has no IPv4 default route")
}

// claimPublicIPv4 makes the host answer ARP for the public address on the public interface.
// The kernel answers a proxy entry only when the address routes through another device.
func claimPublicIPv4(ctx context.Context, userID uint32, publicInterfaceName, publicIPv4 string) error {
	hostVirtualEthernet, _ := virtualEthernetNames(userID)
	return runSteps(ctx, [][]string{
		{"ip", "-4", "route", "replace", publicIPv4 + "/32", "dev", hostVirtualEthernet, "proto", "static"},
		{"ip", "-4", "neigh", "replace", "proxy", publicIPv4, "dev", publicInterfaceName},
	})
}

// releasePublicIPv4Claims removes every public address claim of the VM except keep.
func releasePublicIPv4Claims(ctx context.Context, userID uint32, publicInterfaceName, keep string) error {
	hostVirtualEthernet, _ := virtualEthernetNames(userID)
	exists, err := networkLinkExists(ctx, hostVirtualEthernet)
	if err != nil || !exists {
		return err
	}

	output, err := platform.Output(ctx, "ip", "-4", "route", "show", "dev", hostVirtualEthernet, "proto", "static")
	if err != nil {
		return fmt.Errorf("read the public IPv4 claims of %s: %w", hostVirtualEthernet, err)
	}
	for _, line := range strings.Split(output, "\n") {
		fields := strings.Fields(line)
		if len(fields) == 0 || fields[0] == keep {
			continue
		}
		// The route marks the claim, so it goes only after the proxy entry.
		err := removeProxyNeighbour(ctx, "-4", fields[0], publicInterfaceName)
		if err == nil {
			err = platform.Run(ctx, "ip", "-4", "route", "del", fields[0]+"/32", "dev", hostVirtualEthernet)
		}
		if err != nil {
			return fmt.Errorf("release the public IPv4 claim of %s: %w", fields[0], err)
		}
	}
	return nil
}

// claimPublicIPv6 makes the host answer NDP for the public address on the public interface.
func claimPublicIPv6(ctx context.Context, publicInterfaceName, publicIPv6 string) error {
	return runSteps(ctx, [][]string{
		{"sysctl", "-q", "-w", "net.ipv6.conf." + publicInterfaceName + ".proxy_ndp=1"},
		{"ip", "-6", "neigh", "replace", "proxy", strings.TrimSuffix(publicIPv6, "/128"), "dev", publicInterfaceName},
	})
}

// releasePublicIPv6Claims removes every public address claim of the VM except keep.
// The mesh routes each public /128 through the host veth, so these routes list the claims.
func releasePublicIPv6Claims(ctx context.Context, userID uint32, publicInterfaceName, keep string) error {
	hostVirtualEthernet, _ := virtualEthernetNames(userID)
	exists, err := networkLinkExists(ctx, hostVirtualEthernet)
	if err != nil || !exists {
		return err
	}

	output, err := platform.Output(ctx, "ip", "-6", "route", "show", "dev", hostVirtualEthernet)
	if err != nil {
		return fmt.Errorf("read the public IPv6 claims of %s: %w", hostVirtualEthernet, err)
	}
	for _, line := range strings.Split(output, "\n") {
		fields := strings.Fields(line)
		if len(fields) == 0 || strings.Contains(fields[0], "/") || fieldAfter(fields, "via") == "" || fields[0] == strings.TrimSuffix(keep, "/128") {
			continue
		}
		if err := removeProxyNeighbour(ctx, "-6", fields[0], publicInterfaceName); err != nil {
			return fmt.Errorf("release the public IPv6 claim of %s: %w", fields[0], err)
		}
	}
	return nil
}

// removeProxyNeighbour skips an absent entry, so a retried release does not fail.
func removeProxyNeighbour(ctx context.Context, familyFlag, address, publicInterfaceName string) error {
	output, err := platform.Output(ctx, "ip", familyFlag, "neigh", "show", "proxy", address, "dev", publicInterfaceName)
	if err != nil || strings.TrimSpace(output) == "" {
		return err
	}
	return platform.Run(ctx, "ip", familyFlag, "neigh", "del", "proxy", address, "dev", publicInterfaceName)
}

func ensurePublicIPv6(ctx context.Context, virtualMachineID, publicIPv6, meshIPv6 string) error {
	address := strings.TrimSuffix(publicIPv6, "/128")
	steps := publicIPv6Steps(virtualMachineID, address, meshIPv6)
	return ensureRuleSet(ctx, steps, func() error { return removePublicIPv6Rules(ctx, virtualMachineID) })
}

// ensureRuleSet replaces the full set when any rule is missing.
func ensureRuleSet(ctx context.Context, steps [][]string, remove func() error) error {
	for _, step := range steps {
		exists, err := ruleExists(ctx, ruleCheck(step))
		if err != nil {
			return err
		}
		if exists {
			continue
		}
		if err := remove(); err != nil {
			return err
		}
		return runSteps(ctx, steps)
	}
	return nil
}

// ruleCheck removes an insert position because iptables -C does not accept it.
func ruleCheck(step []string) []string {
	check := append([]string(nil), step...)
	for index, argument := range check {
		switch argument {
		case "-A":
			check[index] = "-C"
			return check
		case "-I":
			check[index] = "-C"
			return append(check[:index+2], check[index+3:]...)
		}
	}
	return check
}

// publicIPv4Steps maps the public address to the guest: DNAT in for forwarded and
// host-originated traffic, SNAT out, forwarding both ways, and a second DNAT inside
// the namespace. The SNAT rule is inserted first, so it wins over any wider
// masquerade rule.
func publicIPv4Steps(virtualMachineID, namespace, guestVirtualEthernet, namespaceIPAddress, publicIPv4 string) [][]string {
	comment := publicIPv4Comment(virtualMachineID)
	return [][]string{
		// Inbound: the public address becomes the namespace transit address.
		{"iptables", "-t", "nat", "-A", "PREROUTING", "-d", publicIPv4, "-m", "comment", "--comment", comment, "-j", "DNAT", "--to-destination", namespaceIPAddress},

		// Host-originated: a local process reaches a co-located VM through its
		// public address. Such a packet skips PREROUTING, so OUTPUT repeats the map.
		{"iptables", "-t", "nat", "-A", "OUTPUT", "-d", publicIPv4, "-m", "comment", "--comment", comment, "-j", "DNAT", "--to-destination", namespaceIPAddress},

		// Insert SNAT first so it wins over namespace masquerading.
		{"iptables", "-t", "nat", "-I", "POSTROUTING", "1", "-s", namespaceIPAddress, "-m", "comment", "--comment", comment, "-j", "SNAT", "--to-source", publicIPv4},

		// Allow new inbound connections and the traffic they establish.
		{"iptables", "-A", "FORWARD", "-d", namespaceIPAddress, "-m", "conntrack", "--ctstate", "NEW,ESTABLISHED,RELATED", "-m", "comment", "--comment", comment, "-j", "ACCEPT"},

		// Allow the return path only. The VM starts no connection through this rule.
		{"iptables", "-A", "FORWARD", "-s", namespaceIPAddress, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-m", "comment", "--comment", comment, "-j", "ACCEPT"},

		// Inside the namespace: the transit address becomes the guest address.
		{"ip", "netns", "exec", namespace, "iptables", "-t", "nat", "-A", "PREROUTING", "-i", guestVirtualEthernet, "-d", namespaceIPAddress, "-m", "comment", "--comment", comment, "-j", "DNAT", "--to-destination", guestIPAddress},
	}
}

// publicIPv6Steps maps a public /128 to the guest mesh address: DNAT in for
// forwarded and host-originated traffic, SNAT out for connections to a public
// address, and forwarding both ways. The mesh address reaches the guest directly,
// so no namespace rule is necessary.
func publicIPv6Steps(virtualMachineID, publicIPv6, meshIPv6 string) [][]string {
	comment := publicIPv6Comment(virtualMachineID)
	return [][]string{
		{"ip6tables", "-t", "nat", "-A", "PREROUTING", "-d", publicIPv6, "-m", "comment", "--comment", comment, "-j", "DNAT", "--to-destination", meshIPv6},
		{"ip6tables", "-t", "nat", "-A", "OUTPUT", "-d", publicIPv6, "-m", "comment", "--comment", comment, "-j", "DNAT", "--to-destination", meshIPv6},

		// Mesh traffic keeps the mesh address. The rule tests the original destination, so a
		// connection to the public address of another VM changes source too. DNAT has
		// already made its destination a mesh address, and the reply must return through
		// this host, because the mesh drops traffic between tenants.
		{"ip6tables", "-t", "nat", "-I", "POSTROUTING", "1", "-s", meshIPv6, "-m", "conntrack", "!", "--ctorigdst", meshPrefix, "-m", "comment", "--comment", comment, "-j", "SNAT", "--to-source", publicIPv6},

		{"ip6tables", "-A", "FORWARD", "-d", meshIPv6, "-m", "conntrack", "--ctstate", "NEW,ESTABLISHED,RELATED", "-m", "comment", "--comment", comment, "-j", "ACCEPT"},
		{"ip6tables", "-A", "FORWARD", "-s", meshIPv6, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-m", "comment", "--comment", comment, "-j", "ACCEPT"},
	}
}

func removePublicIPv6Rules(ctx context.Context, virtualMachineID string) error {
	return removeTaggedRules(ctx, nil, "ip6tables", publicIPv6Comment(virtualMachineID))
}

func removePublicIPv4Rules(ctx context.Context, virtualMachineID string) error {
	return removeTaggedRules(ctx, nil, "iptables", publicIPv4Comment(virtualMachineID))
}

func removePublicIPv4NamespaceRules(ctx context.Context, virtualMachineID string) error {
	return removeTaggedRules(ctx, namespaceCommandPrefix(namespaceName(virtualMachineID)), "iptables", publicIPv4Comment(virtualMachineID))
}

func namespaceCommandPrefix(namespace string) []string {
	return []string{"ip", "netns", "exec", namespace}
}

// removeTaggedRules deletes every rule carrying this comment. It continues past
// a failure and joins the errors, so one bad table does not leave the other
// tables untouched.
func removeTaggedRules(ctx context.Context, prefix []string, command, comment string) error {
	var cleanupErrors []error
	for _, table := range []string{"nat", "filter"} {
		arguments := commandWithPrefix(prefix, command, "-t", table, "-S")
		output, err := platform.Output(ctx, arguments[0], arguments[1:]...)
		if err != nil {
			cleanupErrors = append(cleanupErrors, fmt.Errorf("list %s rules: %w", table, err))
			continue
		}
		for _, line := range strings.Split(output, "\n") {
			ruleArguments := strings.Fields(line)
			if !hasRuleComment(ruleArguments, comment) {
				continue
			}
			if len(ruleArguments) < 2 || ruleArguments[0] != "-A" {
				continue
			}
			ruleArguments[0] = "-D"
			for index, argument := range ruleArguments {
				ruleArguments[index] = strings.Trim(argument, "\"")
			}
			arguments = commandWithPrefix(prefix, command, "-t", table)
			arguments = append(arguments, ruleArguments...)
			if err := platform.Run(ctx, arguments[0], arguments[1:]...); err != nil {
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

func publicIPv6Comment(virtualMachineID string) string {
	return "metal-public-ipv6-" + virtualMachineID
}
