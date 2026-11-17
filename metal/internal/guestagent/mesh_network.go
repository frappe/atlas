package guestagent

import (
	"context"
	"fmt"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
)

const (
	meshDropInPath = "/run/systemd/network/10-atlas.network.d/mesh.conf"
	systemBusPath  = "/run/dbus/system_bus_socket"
	meshGateway    = "fe80::1"
	meshPrefix     = "fdaa::/16"
	// Matches the image routes outside the mesh, which can lead to a 1500-byte network.
	outsideMeshMTU = "1380"
)

// applyMeshNetwork applies the network at once with ip, then keeps it with a networkd
// drop-in. A zero mtuBytes keeps the image MTU.
func applyMeshNetwork(ctx context.Context, address string, routes []string, mtuBytes int) {
	meshAddress, err := netip.ParseAddr(address)
	if err != nil || !meshAddress.Is6() {
		return
	}
	dropIn := meshDropIn(meshAddress, routes, mtuBytes)
	if current, err := os.ReadFile(meshDropInPath); err == nil && string(current) == dropIn {
		return
	}

	// Netlink works before D-Bus starts.
	if mtuBytes != 0 {
		mtu := strconv.Itoa(mtuBytes)
		_ = runCommand(ctx, "ip", "link", "set", "dev", "eth0", "mtu", mtu)
		// The kernel raises each IPv6 route that carried the old link MTU, so pin ::/0 again.
		_ = runCommand(ctx, "ip", "-6", "route", "replace", "default", "via", meshGateway, "dev", "eth0",
			"proto", "static", "metric", "1024", "mtu", outsideMeshMTU)
	}
	_ = runCommand(ctx, "ip", "-6", "address", "replace", meshAddress.String()+"/128", "dev", "eth0", "nodad")
	_ = runCommand(ctx, "ip", "-6", "route", "replace", meshPrefix, "via", meshGateway, "dev", "eth0")
	for _, destination := range routes {
		_ = runCommand(ctx, "ip", "-6", "route", "replace", destination, "via", meshGateway, "dev", "eth0", "mtu", outsideMeshMTU)
	}

	// networkctl needs D-Bus, which starts after this early unit. Each failed call logs an error, so wait for it.
	if _, err := os.Stat(systemBusPath); err != nil {
		return
	}
	if err := os.MkdirAll(filepath.Dir(meshDropInPath), 0o755); err != nil {
		return
	}
	if err := os.WriteFile(meshDropInPath, []byte(dropIn), 0o644); err != nil {
		return
	}
	// Remove the file when networkd is not ready, so the next poll retries.
	if err := runCommand(ctx, "networkctl", "reload"); err != nil {
		_ = os.Remove(meshDropInPath)
		return
	}

	// Reset services that remain backed off after a networkless warm snapshot.
	_ = runCommand(ctx, "resolvectl", "reset-server-features")
	_ = runCommand(ctx, "resolvectl", "flush-caches")
	// Queued, not awaited: time sync takes seconds to stop, and the agent must keep polling.
	_ = runCommand(ctx, "systemctl", "restart", "--no-block", "systemd-timesyncd")
}

func meshDropIn(address netip.Addr, routes []string, mtuBytes int) string {
	dropIn := ""
	if mtuBytes != 0 {
		dropIn = fmt.Sprintf("[Link]\nMTUBytes=%d\n\n", mtuBytes)
	}
	dropIn += fmt.Sprintf("[Network]\nAddress=%s/128\nIPv6AcceptRA=no\n\n[Route]\nDestination=%s\nGateway=%s\n", address, meshPrefix, meshGateway)
	for _, destination := range routes {
		dropIn += fmt.Sprintf("\n[Route]\nDestination=%s\nGateway=%s\nMTUBytes=%s\n", destination, meshGateway, outsideMeshMTU)
	}
	return dropIn
}

func runCommand(ctx context.Context, name string, arguments ...string) error {
	command := exec.CommandContext(ctx, name, arguments...)
	command.Stdout, command.Stderr = os.Stderr, os.Stderr
	return command.Run()
}
