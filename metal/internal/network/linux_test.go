package network

import (
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
