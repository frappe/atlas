package network

import (
	"slices"
	"strings"
	"testing"
)

func TestNewMeshNeedsACommandAndBothInterfaces(t *testing.T) {
	complete := MeshConfig{
		CommandPath:   "/usr/local/bin/atlas-wg-mesh",
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
		CommandPath:   "/usr/local/bin/atlas-wg-mesh",
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
