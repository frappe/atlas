package network

import (
	"strings"
	"testing"

	"github.com/frappe/atlas/metal/internal/idalloc"
)

func TestNames(t *testing.T) {
	if got := nsName("abc"); got != "metal-abc" {
		t.Errorf("nsName = %q", got)
	}
	if got := nsPath("abc"); got != "/run/netns/metal-abc" {
		t.Errorf("nsPath = %q", got)
	}
}

func TestTransitAddrs(t *testing.T) {
	h, n := transitAddrs(100000)
	if h != "10.6.26.129" || n != "10.6.26.130" {
		t.Errorf("transitAddrs(100000) = %q, %q", h, n)
	}
	// distinct uids must not collide
	h2, _ := transitAddrs(100001)
	if h == h2 {
		t.Error("transit addrs collide across uids")
	}
}

func TestMacFor(t *testing.T) {
	m := macFor("abc")
	if m != macFor("abc") {
		t.Error("macFor not deterministic")
	}
	if !strings.HasPrefix(m, "02:") || len(m) != 17 {
		t.Errorf("mac = %q", m)
	}
}

// A veth name must fit the 15 character limit for an interface name, at the
// highest uid the allocator can hand out.
func TestVethNamesFitInterfaceLimit(t *testing.T) {
	host, guest := vethNames(idalloc.DefaultRange.Max)
	if host != "vh-165535" || guest != "vg-165535" {
		t.Errorf("vethNames = %q, %q", host, guest)
	}
	for _, name := range []string{host, guest} {
		if len(name) > 15 {
			t.Errorf("%q is %d characters, over the 15 character limit", name, len(name))
		}
	}
}
