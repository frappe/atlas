package network

import (
	"strings"
	"testing"
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
