package network

import (
	"context"
	"crypto/sha256"
	"fmt"

	"github.com/frappe/atlas/metal/internal/hostcmd"
)

const (
	tapName   = "tap0"
	gatewayIP = "172.16.0.1"
	guestIP   = "172.16.0.2"
	prefixLen = 24
)

// Linux gives each VM its own network namespace with a TAP the guest uses, plus
// a veth uplink to the host for external traffic. The guest-facing addresses are
// fixed (each netns is isolated); the veth transit /30 is derived from the VM's
// uid, which is already unique, so no separate IP allocator is needed. The host
// must have ip_forward and an uplink MASQUERADE in place (see scripts/net-setup.sh).
type Linux struct{}

func NewLinux() *Linux { return &Linux{} }

func nsName(vmID string) string { return "metal-" + vmID }
func nsPath(vmID string) string { return "/run/netns/" + nsName(vmID) }

func vethNames(uid uint32) (host, guest string) {
	return fmt.Sprintf("vh%d", uid), fmt.Sprintf("vg%d", uid)
}

// transitAddrs derives the veth /30 endpoints (host, netns) from the uid.
func transitAddrs(uid uint32) (hostIP, nsIP string) {
	base := uint32(0x0A000000) | ((uid & 0x3FFFFF) << 2) // 10.0.0.0/8 + uid*4
	return ipString(base + 1), ipString(base + 2)
}

func ipString(v uint32) string {
	return fmt.Sprintf("%d.%d.%d.%d", byte(v>>24), byte(v>>16), byte(v>>8), byte(v))
}

func (l *Linux) Allocate(ctx context.Context, req Request) (NIC, error) {
	ns := nsName(req.VMID)
	uid, gid := fmt.Sprint(req.UID), fmt.Sprint(req.GID)
	vh, vg := vethNames(req.UID)
	hostIP, nsIP := transitAddrs(req.UID)
	gwCIDR := fmt.Sprintf("%s/%d", gatewayIP, prefixLen)

	steps := [][]string{
		{"ip", "netns", "add", ns},
		{"ip", "-n", ns, "link", "set", "lo", "up"},
		// TAP the guest attaches to
		{"ip", "-n", ns, "tuntap", "add", tapName, "mode", "tap", "user", uid, "group", gid},
		{"ip", "-n", ns, "addr", "add", gwCIDR, "dev", tapName},
		{"ip", "-n", ns, "link", "set", tapName, "up"},
		// veth uplink: host <-> netns
		{"ip", "link", "add", vh, "type", "veth", "peer", "name", vg},
		{"ip", "link", "set", vg, "netns", ns},
		{"ip", "addr", "add", hostIP + "/30", "dev", vh},
		{"ip", "link", "set", vh, "up"},
		{"ip", "-n", ns, "addr", "add", nsIP + "/30", "dev", vg},
		{"ip", "-n", ns, "link", "set", vg, "up"},
		{"ip", "-n", ns, "route", "add", "default", "via", hostIP},
		// forward + NAT inside the netns, hiding the fixed guest IP behind the uplink
		{"ip", "netns", "exec", ns, "sysctl", "-q", "-w", "net.ipv4.ip_forward=1"},
		{"ip", "netns", "exec", ns, "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", vg, "-j", "MASQUERADE"},
	}
	for _, s := range steps {
		if err := hostcmd.Run(ctx, s[0], s[1:]...); err != nil {
			_ = l.Release(ctx, req.VMID)
			_ = hostcmd.Run(ctx, "ip", "link", "del", vh) // in case the veth was created but not yet moved
			return NIC{}, err
		}
	}
	return l.Resolve(req.VMID), nil
}

// Resolve returns the VM's NIC without touching the system. Every field is
// deterministic (see Allocate), so a stopped VM whose netns still exists can be
// reconfigured from just its id.
func (l *Linux) Resolve(vmID string) NIC {
	return NIC{
		NetnsPath: nsPath(vmID),
		TapName:   tapName,
		MAC:       macFor(vmID),
		GuestIP:   guestIP,
		GatewayIP: gatewayIP,
	}
}

// Release deletes the netns, which tears down the TAP and both veth ends.
func (l *Linux) Release(ctx context.Context, vmID string) error {
	return hostcmd.Run(ctx, "ip", "netns", "del", nsName(vmID))
}

// macFor derives a stable locally-administered unicast MAC from the VM id.
func macFor(vmID string) string {
	h := sha256.Sum256([]byte(vmID))
	return fmt.Sprintf("02:%02x:%02x:%02x:%02x:%02x", h[0], h[1], h[2], h[3], h[4])
}

var _ Allocator = (*Linux)(nil)
