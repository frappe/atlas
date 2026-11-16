package network

import (
	"context"
	"fmt"
	"strings"

	platform "github.com/frappe/atlas/metal/internal/platform"
)

// One tc priority holds one protocol, so IPv4 and IPv6 filters need separate
// priorities. Both private priorities outrank the public one. Without an mtu, a
// tc policer drops every packet over 2 KB, which includes jumbo mesh packets.
const (
	trafficControlBurst        = "1mb"
	trafficControlMTU          = "64kb"
	privateIPv4TrafficPriority = "10"
	privateIPv6TrafficPriority = "11"
	publicTrafficPriority      = "20"
)

// privatePrefixes are the address ranges that the private throughput limit
// matches. The IPv4 ranges are RFC 1918. The IPv6 range is the unique local
// address block, which contains every Atlas mesh prefix.
var privatePrefixes = []struct {
	Protocol string
	Prefix   string
	Priority string
}{
	{"ip", "10.0.0.0/8", privateIPv4TrafficPriority},
	{"ip", "172.16.0.0/12", privateIPv4TrafficPriority},
	{"ip", "192.168.0.0/16", privateIPv4TrafficPriority},
	{"ipv6", "fc00::/7", privateIPv6TrafficPriority},
}

// trafficControlRequest describes the throughput policers for one VM veth.
type trafficControlRequest struct {
	VirtualMachineID              string
	UserID                        uint32
	HasNetworkAttachment          bool
	HasIPv4HostRoute              bool
	PrivateNetworkThroughputMiBps int
	PublicNetworkThroughputMiBps  int
}

// hasVirtualEthernet reports whether the VM has a veth pair that can hold the policers.
func (request trafficControlRequest) hasVirtualEthernet() bool {
	return request.HasNetworkAttachment
}

// configureTrafficControl applies the policers to the namespace end of the veth.
// The host end belongs to Atlas WG Mesh and its terminating direct-action program.
func configureTrafficControl(ctx context.Context, request trafficControlRequest) error {
	if !request.hasVirtualEthernet() {
		return nil
	}

	namespace := namespaceName(request.VirtualMachineID)
	_, guestVirtualEthernet := virtualEthernetNames(request.UserID)
	if err := removeTrafficControl(ctx, namespace, guestVirtualEthernet); err != nil {
		return err
	}

	for _, step := range trafficControlSteps(namespace, guestVirtualEthernet, request) {
		if err := platform.Run(ctx, step[0], step[1:]...); err != nil {
			return err
		}
	}
	return nil
}

// removeTrafficControl drops the existing policers, so a new set replaces them
// instead of stacking on top. tc fails to add a qdisc that is already there.
func removeTrafficControl(ctx context.Context, namespace, interfaceName string) error {
	prefix := namespaceCommandPrefix(namespace)
	show := commandWithPrefix(prefix, "tc", "qdisc", "show", "dev", interfaceName)
	output, err := platform.Output(ctx, show[0], show[1:]...)
	if err != nil {
		return err
	}
	if !strings.Contains(output, "qdisc clsact") {
		return nil
	}

	remove := commandWithPrefix(prefix, "tc", "qdisc", "del", "dev", interfaceName, "clsact")
	return platform.Run(ctx, remove[0], remove[1:]...)
}

// trafficControlSteps builds the tc commands for the namespace end of the veth.
// The private filters take the lower priority, so a private packet never reaches
// the public filter, which matches every IPv4 address.
//
// The VM is inside the namespace: egress carries traffic from it and matches the
// destination, ingress carries traffic to it and matches the source.
func trafficControlSteps(namespace, guestVirtualEthernet string, request trafficControlRequest) [][]string {
	hasPublicLimit := request.PublicNetworkThroughputMiBps > 0 && request.HasIPv4HostRoute
	hasPrivateLimit := request.PrivateNetworkThroughputMiBps > 0
	if !hasPrivateLimit && !hasPublicLimit {
		return nil
	}

	prefix := namespaceCommandPrefix(namespace)
	device := guestVirtualEthernet
	steps := [][]string{commandWithPrefix(prefix, "tc", "qdisc", "add", "dev", device, "clsact")}

	if hasPrivateLimit {
		rate := rateArgument(request.PrivateNetworkThroughputMiBps)
		for _, private := range privatePrefixes {
			steps = append(steps,
				policeFilter(prefix, device, "egress", private.Protocol, private.Priority, "dst_ip", private.Prefix, rate),
				policeFilter(prefix, device, "ingress", private.Protocol, private.Priority, "src_ip", private.Prefix, rate),
			)
		}
	}

	if hasPublicLimit {
		rate := rateArgument(request.PublicNetworkThroughputMiBps)
		steps = append(steps,
			policeFilter(prefix, device, "egress", "ip", publicTrafficPriority, "", "", rate),
			policeFilter(prefix, device, "ingress", "ip", publicTrafficPriority, "", "", rate),
		)
	}

	return steps
}

// policeFilter builds one tc filter that polices matching traffic to rate. An
// empty matchPrefix matches every address, which is how the public limit works.
// Traffic above the rate is dropped, not queued, so a VM cannot build a backlog.
func policeFilter(prefix []string, device, direction, protocol, priority, matchField, matchPrefix, rate string) []string {
	arguments := []string{
		"filter", "add", "dev", device, direction,
		"protocol", protocol, "prio", priority,
		"flower",
	}
	if matchPrefix != "" {
		arguments = append(arguments, matchField, matchPrefix)
	}
	arguments = append(arguments,
		"action", "police", "rate", rate, "burst", trafficControlBurst, "mtu", trafficControlMTU,
		"conform-exceed", "drop/ok",
	)

	return commandWithPrefix(prefix, "tc", arguments...)
}

// rateArgument formats a throughput limit the way tc expects it.
func rateArgument(throughputMiBps int) string {
	return fmt.Sprintf("%dmibps", throughputMiBps)
}
