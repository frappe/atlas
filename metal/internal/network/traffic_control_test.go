package network

import (
	"reflect"
	"slices"
	"testing"

	"github.com/frappe/atlas/metal/internal/vm"
)

func TestTrafficControlStepsLimitPrivateAndPublicTraffic(t *testing.T) {
	request := trafficControlRequest{
		Egress:                        vm.EgressUplink,
		PrivateNetworkThroughputMiBps: 100,
		PublicNetworkThroughputMiBps:  50,
	}

	got := trafficControlSteps("metal-vm-1", "vg-1000", request)
	want := [][]string{
		{"ip", "netns", "exec", "metal-vm-1", "tc", "qdisc", "add", "dev", "vg-1000", "clsact"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "egress", "protocol", "ip", "prio", "10", "flower", "dst_ip", "10.0.0.0/8", "action", "police", "rate", "100mibps", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "ingress", "protocol", "ip", "prio", "10", "flower", "src_ip", "10.0.0.0/8", "action", "police", "rate", "100mibps", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "egress", "protocol", "ip", "prio", "10", "flower", "dst_ip", "172.16.0.0/12", "action", "police", "rate", "100mibps", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "ingress", "protocol", "ip", "prio", "10", "flower", "src_ip", "172.16.0.0/12", "action", "police", "rate", "100mibps", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "egress", "protocol", "ip", "prio", "10", "flower", "dst_ip", "192.168.0.0/16", "action", "police", "rate", "100mibps", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "ingress", "protocol", "ip", "prio", "10", "flower", "src_ip", "192.168.0.0/16", "action", "police", "rate", "100mibps", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "egress", "protocol", "ipv6", "prio", "11", "flower", "dst_ip", "fc00::/7", "action", "police", "rate", "100mibps", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "ingress", "protocol", "ipv6", "prio", "11", "flower", "src_ip", "fc00::/7", "action", "police", "rate", "100mibps", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "egress", "protocol", "ip", "prio", "20", "flower", "action", "police", "rate", "50mibps", "burst", "1mb", "conform-exceed", "drop/ok"},
		{"ip", "netns", "exec", "metal-vm-1", "tc", "filter", "add", "dev", "vg-1000", "ingress", "protocol", "ip", "prio", "20", "flower", "action", "police", "rate", "50mibps", "burst", "1mb", "conform-exceed", "drop/ok"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("traffic control steps = %#v, want %#v", got, want)
	}
}

// The public filter matches every IPv4 address, so the private filters must keep
// the lower priority to classify RFC 1918 traffic first.
func TestPrivateFiltersOutrankThePublicFilter(t *testing.T) {
	steps := trafficControlSteps("metal-vm-1", "vg-1000", trafficControlRequest{
		Egress:                        vm.EgressUplink,
		PrivateNetworkThroughputMiBps: 100,
		PublicNetworkThroughputMiBps:  50,
	})

	// One tc priority holds one protocol, so a priority must never carry two.
	protocolByPriority := map[string]string{}
	for _, step := range steps[1:] {
		priority := step[slices.Index(step, "prio")+1]
		protocol := step[slices.Index(step, "protocol")+1]
		hasPrefix := slices.Contains(step, "dst_ip") || slices.Contains(step, "src_ip")

		if existing, seen := protocolByPriority[priority]; seen && existing != protocol {
			t.Fatalf("priority %s carries %s and %s", priority, existing, protocol)
		}
		protocolByPriority[priority] = protocol

		wanted := publicTrafficPriority
		if hasPrefix {
			wanted = privateIPv4TrafficPriority
			if protocol == "ipv6" {
				wanted = privateIPv6TrafficPriority
			}
		}
		if priority != wanted {
			t.Fatalf("filter %v uses priority %s, want %s", step, priority, wanted)
		}
	}
	for _, private := range []string{privateIPv4TrafficPriority, privateIPv6TrafficPriority} {
		if private >= publicTrafficPriority {
			t.Fatalf("private priority %s must sort before %s", private, publicTrafficPriority)
		}
	}
}

func TestTrafficControlStepsSkipUnlimitedTraffic(t *testing.T) {
	if got := trafficControlSteps("metal-vm-1", "vg-1000", trafficControlRequest{}); got != nil {
		t.Fatalf("traffic control steps = %#v, want none", got)
	}
}

// Only EgressNone removes the veth pair, so mesh VMs still hold the policers.
func TestTrafficControlNeedsVirtualEthernet(t *testing.T) {
	for _, testCase := range []struct {
		egress vm.Egress
		want   bool
	}{
		{vm.EgressUplink, true},
		{vm.EgressMesh, true},
		{vm.EgressNone, false},
	} {
		request := trafficControlRequest{Egress: testCase.egress}
		if got := request.hasVirtualEthernet(); got != testCase.want {
			t.Fatalf("egress %q has veth = %v, want %v", testCase.egress, got, testCase.want)
		}
	}
}

// A mesh VM has no internet path, so a public policer could never match.
func TestTrafficControlSkipsThePublicLimitWithoutAnInternetPath(t *testing.T) {
	request := trafficControlRequest{
		Egress:                        vm.EgressMesh,
		PrivateNetworkThroughputMiBps: 100,
		PublicNetworkThroughputMiBps:  50,
	}

	for _, step := range trafficControlSteps("metal-vm-1", "vg-1000", request) {
		if slices.Contains(step, "50mibps") {
			t.Fatalf("mesh egress installed a public policer: %v", step)
		}
	}

	// A public limit alone leaves nothing to install, not even the qdisc.
	if got := trafficControlSteps("metal-vm-1", "vg-1000", trafficControlRequest{
		Egress:                       vm.EgressMesh,
		PublicNetworkThroughputMiBps: 50,
	}); got != nil {
		t.Fatalf("mesh egress with only a public limit = %#v, want none", got)
	}
}
