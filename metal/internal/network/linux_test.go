package network

import (
	"context"
	"errors"
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

func TestGuestMACAddressIsTheSameForEveryVirtualMachine(t *testing.T) {
	allocator := NewLinuxAllocator(nil)
	address := allocator.Resolve("vm-1").MACAddress
	if address != allocator.Resolve("vm-2").MACAddress {
		t.Error("MAC address differs between virtual machines")
	}
	if address != "06:00:ac:10:00:02" {
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

type fakeMesh struct {
	added     []string
	removed   []string
	removeErr error
}

func (mesh *fakeMesh) Add(_ context.Context, address, interfaceName string) error {
	mesh.added = append(mesh.added, address+" "+interfaceName)
	return nil
}

func (mesh *fakeMesh) Remove(_ context.Context, address, interfaceName string) error {
	mesh.removed = append(mesh.removed, address+" "+interfaceName)
	return mesh.removeErr
}

func TestRemoveMeshRegistrationNamesTheHostVirtualEthernet(t *testing.T) {
	mesh := &fakeMesh{}
	allocator := &LinuxAllocator{mesh: mesh}

	if err := allocator.removeMeshRegistration(context.Background(), 100000, "fdaa:1:0:1::1"); err != nil {
		t.Fatal(err)
	}
	if want := []string{"fdaa:1:0:1::1 vh-100000"}; !slices.Equal(mesh.removed, want) {
		t.Errorf("removed = %v, want %v", mesh.removed, want)
	}
}

func TestMeshRegistrationSkipsAVirtualMachineWithoutAnAddress(t *testing.T) {
	mesh := &fakeMesh{}
	allocator := &LinuxAllocator{mesh: mesh}

	if err := allocator.removeMeshRegistration(context.Background(), 100000, ""); err != nil {
		t.Fatal(err)
	}
	if err := allocator.addMeshRegistration(context.Background(), "vm-1", 100000, ""); err != nil {
		t.Fatal(err)
	}
	if len(mesh.removed) != 0 || len(mesh.added) != 0 {
		t.Errorf("mesh calls = %v and %v, want none", mesh.added, mesh.removed)
	}
}

// A typed nil inside an interface is not a nil interface.
func TestNewLinuxAllocatorWithoutAMeshMakesNoMeshCalls(t *testing.T) {
	allocator := NewLinuxAllocator(nil)

	if allocator.mesh != nil {
		t.Fatal("a nil mesh produced a non-nil registrar")
	}
	if err := allocator.removeMeshRegistration(context.Background(), 100000, "fdaa:1:0:1::1"); err != nil {
		t.Errorf("removeMeshRegistration = %v", err)
	}
}

func TestRemoveMeshRegistrationWrapsTheRegistrarError(t *testing.T) {
	mesh := &fakeMesh{removeErr: errors.New("not registered")}
	allocator := &LinuxAllocator{mesh: mesh}

	err := allocator.removeMeshRegistration(context.Background(), 100000, "fdaa:1:0:1::1")
	if err == nil || !strings.Contains(err.Error(), "fdaa:1:0:1::1") {
		t.Errorf("error = %v, want the address", err)
	}
}
