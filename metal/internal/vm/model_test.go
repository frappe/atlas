package vm

import (
	"slices"
	"testing"
)

func TestSleepingIsNotAVirtualMachineState(t *testing.T) {
	if isObservedState(State("sleeping")) || IsDesiredState(State("sleeping")) {
		t.Error("sleeping must not be a virtual machine state")
	}
}

func TestRoutesDecideHostPaths(t *testing.T) {
	network := NetworkConfiguration{
		WireGuardMeshIPv6: "fdaa:1::7",
		Routes: []Route{
			{Destination: "0.0.0.0/0", Via: RouteViaHost},
			{Destination: "2000::/3", Via: "fdaa:1::56"},
		},
	}

	if !network.HasNetworkAttachment() || !network.HasIPv4HostRoute() {
		t.Fatal("an attached VM with an IPv4 host route must report both")
	}
	if network.HasIPv6HostRoute() {
		t.Fatal("a gateway route must not count as an IPv6 host route")
	}
	if got := network.RoutesViaGateway(); !slices.Equal(got, []Route{{Destination: "2000::/3", Via: "fdaa:1::56"}}) {
		t.Fatalf("gateway routes = %v", got)
	}
}

func TestEmptyNetworkHasNoAttachment(t *testing.T) {
	if (NetworkConfiguration{}).HasNetworkAttachment() {
		t.Fatal("an empty network must not have an attachment")
	}
}

func TestOnlyOneAddressIsAPublicIPv6Address(t *testing.T) {
	if !(NetworkConfiguration{PublicIPv6: "2001:db8::7/128"}).HasPublicIPv6Address() {
		t.Fatal("a /128 must be a public IPv6 address")
	}
	if (NetworkConfiguration{PublicIPv6: "2001:db8::/64"}).HasPublicIPv6Address() {
		t.Fatal("a /64 must stay a routed block")
	}
}

func TestDiskEncryptionIsValid(t *testing.T) {
	for _, encryption := range []DiskEncryption{"", DiskEncryptionLUKS2} {
		if !encryption.IsValid() {
			t.Fatalf("disk encryption %q must be valid", encryption)
		}
	}
	if DiskEncryption("luks1").IsValid() {
		t.Fatal("LUKS1 must not be a valid disk encryption mode")
	}
}

func TestSameReservationIncludesDiskEncryptionAndInitrdDigest(t *testing.T) {
	first := testSpecification()
	first.DiskEncryption = DiskEncryptionLUKS2
	first.Image.InitrdSHA256 = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
	second := first
	second.Image.InitrdSHA256 = "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
	if !first.SameReservation(second) {
		t.Fatal("digest case changed the reservation")
	}

	second.DiskEncryption = ""
	if first.SameReservation(second) {
		t.Fatal("disk encryption did not change the reservation")
	}
	second = first
	second.Image.InitrdSHA256 = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
	if first.SameReservation(second) {
		t.Fatal("initrd digest did not change the reservation")
	}
}

func TestRefreshImageSourceReplacesTheInitrdURL(t *testing.T) {
	first := testSpecification()
	first.Image.InitrdURL = "https://example.com/initrd?token=first"
	second := first
	second.Image.InitrdURL = "https://example.com/initrd?token=second"

	refreshed := first.RefreshImageSource(second)

	if refreshed.Image.InitrdURL != second.Image.InitrdURL {
		t.Fatal("signed initrd URL was not refreshed")
	}
}

func TestVirtualCPUCountRoundsMillicoresUp(t *testing.T) {
	for _, testCase := range []struct {
		cpuMillicores int
		want          int
	}{
		{100, 1},
		{999, 1},
		{1000, 1},
		{1001, 2},
		{32000, 32},
	} {
		specification := Specification{CPUMillicores: testCase.cpuMillicores}
		if got := specification.VirtualCPUCount(); got != testCase.want {
			t.Errorf("VirtualCPUCount(%d) = %d, want %d", testCase.cpuMillicores, got, testCase.want)
		}
	}
}

func TestFirewallChangesTheReservation(t *testing.T) {
	first := Specification{Network: NetworkConfiguration{Firewall: FirewallConfiguration{Enabled: false}}}
	second := first
	second.Network.Firewall.Enabled = true

	if first.SameReservation(second) {
		t.Fatal("different firewalls have the same reservation")
	}
}

func TestCloneSpecificationCopiesFirewallRules(t *testing.T) {
	original := Specification{
		Network: NetworkConfiguration{
			Firewall: FirewallConfiguration{
				Inbound: []FirewallRule{{
					Protocol: FirewallProtocolTCP,
					Ports:    "22",
					CIDRs:    []string{"203.0.113.0/24"},
				}},
			},
		},
	}
	cloned := cloneSpecification(original)
	cloned.Network.Firewall.Inbound[0].CIDRs[0] = "198.51.100.0/24"
	cloned.Network.Firewall.Inbound[0].Ports = "443"

	if original.Network.Firewall.Inbound[0].CIDRs[0] != "203.0.113.0/24" {
		t.Fatal("clone shares firewall CIDRs with the source")
	}
	if original.Network.Firewall.Inbound[0].Ports != "22" {
		t.Fatal("clone shares firewall rules with the source")
	}
}

func TestHostReachedDestinationsListIPv6Routes(t *testing.T) {
	network := NetworkConfiguration{Routes: []Route{
		{Destination: "0.0.0.0/0", Via: RouteViaHost},
		{Destination: "2000::/3", Via: RouteViaHost},
		{Destination: "fdac::/16", Via: "fdaa:1::2"},
	}}

	if got := HostReachedDestinations(network); !slices.Equal(got, []string{"2000::/3", "fdac::/16"}) {
		t.Fatalf("destinations = %v", got)
	}
	if got := HostReachedDestinations(NetworkConfiguration{}); len(got) != 0 {
		t.Fatalf("a VM without routes reached %v", got)
	}
}
