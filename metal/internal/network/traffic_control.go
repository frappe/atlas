package network

import (
	"context"
	"fmt"
	"strings"

	"github.com/frappe/atlas/metal/internal/hostcmd"
	"github.com/frappe/atlas/metal/internal/vm"
)

const (
	trafficControlBurst    = "1mb"
	privateTrafficPriority = "10"
	publicTrafficPriority  = "20"
)

// privatePrefixes are the address ranges that the private throughput limit
// matches. The IPv4 ranges are RFC 1918. The IPv6 range is the unique local
// address block, which contains every Atlas mesh prefix.
var privatePrefixes = []struct {
	Protocol string
	Prefix   string
}{
	{"ip", "10.0.0.0/8"},
	{"ip", "172.16.0.0/12"},
	{"ip", "192.168.0.0/16"},
	{"ipv6", "fc00::/7"},
}

// trafficControlRequest describes the throughput policers for one VM veth.
type trafficControlRequest struct {
	VirtualMachineID             string
	UserID                       uint32
	Egress                       vm.Egress
	PrivateNetworkThroughputMbps int
	PublicNetworkThroughputMbps  int
}

// hasVirtualEthernet reports whether the VM has a veth pair that can hold the policers.
func (request trafficControlRequest) hasVirtualEthernet() bool {
	return request.Egress.HasVirtualEthernet()
}

// configureTrafficControl applies the policers to the namespace end of the veth.
// The host end belongs to Atlas WG Mesh, which attaches a terminating
// direct-action program there. A shared hook would let one of the two run.
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
		if err := hostcmd.Run(ctx, step[0], step[1:]...); err != nil {
			return err
		}
	}
	return nil
}

func removeTrafficControl(ctx context.Context, namespace, interfaceName string) error {
	prefix := namespaceCommandPrefix(namespace)
	show := commandWithPrefix(prefix, "tc", "qdisc", "show", "dev", interfaceName)
	output, err := hostcmd.Output(ctx, show[0], show[1:]...)
	if err != nil {
		return err
	}
	if !strings.Contains(output, "qdisc clsact") {
		return nil
	}

	remove := commandWithPrefix(prefix, "tc", "qdisc", "del", "dev", interfaceName, "clsact")
	return hostcmd.Run(ctx, remove[0], remove[1:]...)
}

// trafficControlSteps builds the tc commands for the namespace end of the veth.
// The private filters use the lower priority, so a private packet stops at the
// private policer and never reaches the public filter, which matches every IPv4
// address.
//
// The VM is inside the namespace, so egress carries traffic from the VM and the
// remote end is the destination. Ingress carries traffic to the VM and the two
// are reversed.
func trafficControlSteps(namespace, guestVirtualEthernet string, request trafficControlRequest) [][]string {
	hasPublicLimit := request.PublicNetworkThroughputMbps > 0 && request.Egress.HasInternetPath()
	if request.PrivateNetworkThroughputMbps == 0 && !hasPublicLimit {
		return nil
	}

	prefix := namespaceCommandPrefix(namespace)
	steps := [][]string{commandWithPrefix(prefix, "tc", "qdisc", "add", "dev", guestVirtualEthernet, "clsact")}

	if request.PrivateNetworkThroughputMbps > 0 {
		privateRate := fmt.Sprintf("%dmbit", request.PrivateNetworkThroughputMbps)
		for _, private := range privatePrefixes {
			steps = append(steps,
				commandWithPrefix(prefix,
					"tc", "filter", "add", "dev", guestVirtualEthernet, "egress",
					"protocol", private.Protocol, "prio", privateTrafficPriority,
					"flower", "dst_ip", private.Prefix,
					"action", "police", "rate", privateRate, "burst", trafficControlBurst,
					"conform-exceed", "drop/ok",
				),
				commandWithPrefix(prefix,
					"tc", "filter", "add", "dev", guestVirtualEthernet, "ingress",
					"protocol", private.Protocol, "prio", privateTrafficPriority,
					"flower", "src_ip", private.Prefix,
					"action", "police", "rate", privateRate, "burst", trafficControlBurst,
					"conform-exceed", "drop/ok",
				),
			)
		}
	}

	if request.PublicNetworkThroughputMbps > 0 && request.Egress.HasInternetPath() {
		publicRate := fmt.Sprintf("%dmbit", request.PublicNetworkThroughputMbps)
		steps = append(steps,
			commandWithPrefix(prefix,
				"tc", "filter", "add", "dev", guestVirtualEthernet, "egress",
				"protocol", "ip", "prio", publicTrafficPriority,
				"flower",
				"action", "police", "rate", publicRate, "burst", trafficControlBurst,
				"conform-exceed", "drop/ok",
			),
			commandWithPrefix(prefix,
				"tc", "filter", "add", "dev", guestVirtualEthernet, "ingress",
				"protocol", "ip", "prio", publicTrafficPriority,
				"flower",
				"action", "police", "rate", publicRate, "burst", trafficControlBurst,
				"conform-exceed", "drop/ok",
			),
		)
	}
	return steps
}
