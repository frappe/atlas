package network

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// stubMeshCommand returns an executable path, because NewMesh checks that the
// CLI is on the host.
func stubMeshCommand(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "atlas-wg-mesh")
	if err := os.WriteFile(path, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestNewMeshNeedsACommandAndBothInterfaces(t *testing.T) {
	complete := MeshConfig{
		CommandPath:   stubMeshCommand(t),
		WireGuardName: "wg0",
		UplinkName:    "eno1.1878",
	}

	for name, incomplete := range map[string]MeshConfig{
		"no command path": {WireGuardName: complete.WireGuardName, UplinkName: complete.UplinkName},
		"no WireGuard":    {CommandPath: complete.CommandPath, UplinkName: complete.UplinkName},
		"no uplink":       {CommandPath: complete.CommandPath, WireGuardName: complete.WireGuardName},
	} {
		if _, err := NewMesh(incomplete); err == nil {
			t.Errorf("accepted a configuration with %s", name)
		}
	}
	if _, err := NewMesh(complete); err != nil {
		t.Errorf("NewMesh = %v", err)
	}
}

func TestMeshNamespaceStepsRouteTheGuestAddress(t *testing.T) {
	steps := meshNamespaceSteps("metal-vm-1", "vg-100", "fdaa:1:0:7::1")

	lines := make([]string, len(steps))
	for index, step := range steps {
		lines[index] = strings.Join(step, " ")
	}
	joined := strings.Join(lines, "\n")

	for _, wanted := range []string{
		"sysctl -q -w net.ipv6.conf.all.forwarding=1",
		"sysctl -q -w net.ipv6.conf.vg-100.proxy_ndp=1",
		"link set vg-100 mtu 1380",
		"addr replace fe80::1/64 dev tap0 nodad",
		"route replace fdaa:1:0:7::1/128 dev tap0",
		"route replace fdaa::/16 via fe80::1 dev vg-100",
		"neigh replace proxy fdaa:1:0:7::1 dev vg-100",
	} {
		if !strings.Contains(joined, wanted) {
			t.Errorf("missing step %q in:\n%s", wanted, joined)
		}
	}
}

func TestMeshNamespaceStepsRunInsideTheNamespace(t *testing.T) {
	for _, step := range meshNamespaceSteps("metal-vm-1", "vg-100", "fdaa:1:0:7::1") {
		if !slices.Contains(step, "metal-vm-1") {
			t.Errorf("step leaves the namespace: %v", step)
		}
	}
}

func TestDiscoveryInterfaceReadsTheStatusLine(t *testing.T) {
	status := "discovery interface: eno1.1878 (multicast)\nlocal VMs: 5\nWireGuard address: fdab:1::7\n"
	if name := discoveryInterface(status); name != "eno1.1878" {
		t.Errorf("discoveryInterface = %q", name)
	}
	if name := discoveryInterface("local VMs: 0\n"); name != "" {
		t.Errorf("discoveryInterface without the line = %q", name)
	}
}

func TestVerifyDiscoveryInterfaceRejectsAnotherInterface(t *testing.T) {
	mesh, err := NewMesh(MeshConfig{
		CommandPath:   stubMeshCommand(t),
		WireGuardName: "wg0",
		UplinkName:    "eno1.1878",
	})
	if err != nil {
		t.Fatal(err)
	}

	if err := mesh.verifyDiscoveryInterface("discovery interface: eno1.1878 (multicast)\n"); err != nil {
		t.Errorf("matching interface = %v", err)
	}
	if err := mesh.verifyDiscoveryInterface("discovery interface: eno1 (multicast)\n"); err == nil {
		t.Error("accepted a host that discovers on the parent interface")
	}
	if err := mesh.verifyDiscoveryInterface("local VMs: 0\n"); err == nil {
		t.Error("accepted a status with no discovery interface")
	}
}

func TestParseMeshAddressesNormalisesTheSet(t *testing.T) {
	addresses, err := parseMeshAddresses([]string{"fdaa:1:0:0::1", "fdaa:0001:0000:0000:0000:0000:0000:0002"})
	if err != nil {
		t.Fatal(err)
	}
	for _, wanted := range []string{"fdaa:1::1", "fdaa:1::2"} {
		if _, found := addresses[wanted]; !found {
			t.Errorf("missing %s in %v", wanted, addresses)
		}
	}
	if _, err := parseMeshAddresses([]string{"not-an-address"}); err == nil {
		t.Error("accepted an invalid address")
	}
}

func TestNewMeshNeedsTheCommandOnTheHost(t *testing.T) {
	if _, err := NewMesh(MeshConfig{
		CommandPath:   "/nonexistent/atlas-wg-mesh",
		WireGuardName: "wg0",
		UplinkName:    "eno1.1878",
	}); err == nil {
		t.Error("accepted a command path that is not on the host")
	}
}
