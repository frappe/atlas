package network

import (
	"reflect"
	"slices"
	"testing"

	"github.com/frappe/atlas/metal/internal/vm"
)

func TestTrafficControlStepsLimitPrivateAndPublicTraffic(t *testing.T) {
	request := trafficControlRequest{
		PrivateNetworkThroughputMbps: 100,
		PublicNetworkThroughputMbps:  50,
	}

	got := trafficControlSteps("metal-vm-1", "vg-1000", request)
	want := [][]string{
		{"ip", "netns", "exec", "metal-vm-1", "tc", "qdisc", "add", "dev", "vg-1000", "clsact"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "egress", "protocol", "ip", "prio", "10", "flower", "dst_ip", "10.0.0.0/8", "action", "police", "rate", "100mbit", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "ingress", "protocol", "ip", "prio", "10", "flower", "src_ip", "10.0.0.0/8", "action", "police", "rate", "100mbit", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "egress", "protocol", "ip", "prio", "10", "flower", "dst_ip", "172.16.0.0/12", "action", "police", "rate", "100mbit", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "ingress", "protocol", "ip", "prio", "10", "flower", "src_ip", "172.16.0.0/12", "action", "police", "rate", "100mbit", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "egress", "protocol", "ip", "prio", "10", "flower", "dst_ip", "192.168.0.0/16", "action", "police", "rate", "100mbit", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "ingress", "protocol", "ip", "prio", "10", "flower", "src_ip", "192.168.0.0/16", "action", "police", "rate", "100mbit", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "egress", "protocol", "ipv6", "prio", "10", "flower", "dst_ip", "fc00::/7", "action", "police", "rate", "100mbit", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "ingress", "protocol", "ipv6", "prio", "10", "flower", "src_ip", "fc00::/7", "action", "police", "rate", "100mbit", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "egress", "protocol", "ip", "prio", "20", "flower", "action", "police", "rate", "50mbit", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "ingress", "protocol", "ip", "prio", "20", "flower", "action", "police", "rate", "50mbit", "burst", "1mb", "conform-exceed", "drop/ok"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("traffic control steps = %#v, want %#v", got, want)
	}
}

// The public filter matches every IPv4 address, so the private filters must keep
// the lower priority to classify RFC 1918 traffic first.
func TestPrivateFiltersOutrankThePublicFilter(t *testing.T) {
	steps := trafficControlSteps("metal-vm-1", "vg-1000", trafficControlRequest{
		PrivateNetworkThroughputMbps: 100,
		PublicNetworkThroughputMbps:  50,
	})

	for _, step := range steps[1:] {
		priority := step[slices.Index(step, "prio")+1]
		hasPrefix := slices.Contains(step, "dst_ip") || slices.Contains(step, "src_ip")
		if hasPrefix && priority != privateTrafficPriority {
			t.Fatalf("private filter %v uses priority %s", step, priority)
		}
		if !hasPrefix && priority != publicTrafficPriority {
			t.Fatalf("public filter %v uses priority %s", step, priority)
		}
	}
	if privateTrafficPriority >= publicTrafficPriority {
		t.Fatalf("private priority %s must sort before %s", privateTrafficPriority, publicTrafficPriority)
	}
}

func TestTrafficControlStepsSkipUnlimitedTraffic(t *testing.T) {
	if got := trafficControlSteps("metal-vm-1", "vg-1000", trafficControlRequest{}); got != nil {
		t.Fatalf("traffic control steps = %#v, want none", got)
	}
}

// A VM without host egress has no host veth, so it cannot hold the policers.
func TestTrafficControlNeedsHostEgress(t *testing.T) {
	limited := trafficControlRequest{PublicNetworkThroughputMbps: 50}
	if !limited.hasVirtualEthernet() {
		t.Fatal("empty egress must default to host")
	}

	limited.Egress = vm.EgressNone
	if limited.hasVirtualEthernet() {
		t.Fatal("egress none must skip traffic control")
	}
	limited.Egress = vm.EgressHost
	if !limited.hasVirtualEthernet() {
		t.Fatal("egress host must apply traffic control")
	}
}

func TestEffectiveEgressDefaultsToHost(t *testing.T) {
	if got := effectiveEgress(""); got != "host" {
		t.Fatalf("effective egress = %q, want host", got)
	}
}
