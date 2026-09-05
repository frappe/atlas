package network

import (
	"slices"
	"strings"
	"testing"

	"github.com/frappe/atlas/metal/internal/idalloc"
)

func TestNamespaceNames(t *testing.T) {
	if got := namespaceName("abc"); got != "metal-abc" {
		t.Errorf("namespaceName = %q", got)
	}
	if got := namespacePath("abc"); got != "/run/netns/metal-abc" {
		t.Errorf("namespacePath = %q", got)
	}
}

func TestTransitAddresses(t *testing.T) {
	hostAddress, namespaceAddress := transitAddresses(100000)
	if hostAddress != "10.6.26.129" || namespaceAddress != "10.6.26.130" {
		t.Errorf("transitAddresses(100000) = %q, %q", hostAddress, namespaceAddress)
	}

	secondHostAddress, _ := transitAddresses(100001)
	if hostAddress == secondHostAddress {
		t.Error("transit addresses collide across user IDs")
	}
}

func TestMACAddressIsDeterministic(t *testing.T) {
	address := macAddressFor("abc")
	if address != macAddressFor("abc") {
		t.Error("MAC address is not deterministic")
	}
	if !strings.HasPrefix(address, "02:") || len(address) != 17 {
		t.Errorf("MAC address = %q", address)
	}
}

func TestRuleCommentMatchesExactly(t *testing.T) {
	arguments := []string{"-A", "PREROUTING", "--comment", "metal-public-ipv4-vm-10"}

	if !hasRuleComment(arguments, "metal-public-ipv4-vm-10") {
		t.Fatal("exact comment did not match")
	}
	if hasRuleComment(arguments, "metal-public-ipv4-vm-1") {
		t.Fatal("comment prefix matched another VM")
	}
}

func TestVirtualEthernetNamesFitInterfaceLimit(t *testing.T) {
	hostName, guestName := virtualEthernetNames(idalloc.DefaultRange.Max)
	if hostName != "vh-165535" || guestName != "vg-165535" {
		t.Errorf("virtualEthernetNames = %q, %q", hostName, guestName)
	}

	for _, name := range []string{hostName, guestName} {
		if len(name) > 15 {
			t.Errorf("%q is %d characters, over the 15 character limit", name, len(name))
		}
	}
}

// A failed update rolls back by replaying the reverse transition, so every step
// must be safe to repeat. A plain "route add" fails when the route is present.
func TestDefaultRouteStepsAreSafeToRepeat(t *testing.T) {
	steps := defaultRouteSteps("metal-vm-1", "10.0.0.1")

	if !slices.Contains(steps[0], "replace") || slices.Contains(steps[0], "add") {
		t.Fatalf("default route step = %v, want replace", steps[0])
	}
}

// The internet path keeps the route steps, so one caller cannot drift from the other.
func TestInternetPathStepsExtendTheDefaultRoute(t *testing.T) {
	route := defaultRouteSteps("metal-vm-1", "10.0.0.1")
	steps := internetPathSteps("metal-vm-1", "vg-1000", "10.0.0.1")

	if len(steps) != len(route)+1 {
		t.Fatalf("internet path steps = %d, want %d", len(steps), len(route)+1)
	}
	for index, step := range route {
		if !slices.Equal(steps[index], step) {
			t.Fatalf("step %d = %v, want %v", index, steps[index], step)
		}
	}
	if !slices.Contains(steps[len(steps)-1], "MASQUERADE") {
		t.Fatalf("last step = %v, want MASQUERADE", steps[len(steps)-1])
	}
}
