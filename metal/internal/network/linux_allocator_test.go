package network

import (
	"context"
	"errors"
	"slices"
	"strings"
	"testing"
	"time"

	traffic "github.com/frappe/atlas/metal/internal/network/traffic"
	"github.com/frappe/atlas/metal/internal/vm"
)

// fakeTrafficMonitor records traffic monitor calls.
type fakeTrafficMonitor struct {
	attached []traffic.AttachmentRequest
	detached []string
}

func (monitor *fakeTrafficMonitor) Attach(request traffic.AttachmentRequest) error {
	monitor.attached = append(monitor.attached, request)
	return nil
}

func (monitor *fakeTrafficMonitor) Detach(virtualMachineID string) error {
	monitor.detached = append(monitor.detached, virtualMachineID)
	return nil
}

func TestTrafficTrackingFollowsTheRequestedSetting(t *testing.T) {
	monitor := &fakeTrafficMonitor{}
	allocator := newLinuxAllocator(nil, monitor, nil)
	request := request{VirtualMachineID: "vm-1", UserID: 1001}

	if err := allocator.convergeTrafficMonitoring(true, false, request); err != nil {
		t.Fatal(err)
	}
	if len(monitor.attached) != 1 || monitor.attached[0].Target.VirtualMachineID != request.VirtualMachineID {
		t.Fatalf("attachments = %+v", monitor.attached)
	}
	if err := allocator.convergeTrafficMonitoring(false, false, request); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(monitor.detached, []string{request.VirtualMachineID}) {
		t.Fatalf("detached = %v", monitor.detached)
	}
}

func TestGuestMACAddressIsTheSameForEveryVirtualMachine(t *testing.T) {
	allocator := &LinuxAllocator{}
	address := allocator.interfaceFor("vm-1").MACAddress
	if address != allocator.interfaceFor("vm-2").MACAddress {
		t.Error("MAC address differs between virtual machines")
	}
	if address != "06:00:ac:10:00:02" {
		t.Errorf("MAC address = %q", address)
	}
}

func TestTrafficTrackingIsSkippedWithoutAMonitor(t *testing.T) {
	allocator := NewLinuxAllocator(nil, nil, nil)
	if err := allocator.convergeTrafficMonitoring(true, false, request{VirtualMachineID: "vm-1", UserID: 1001}); err != nil {
		t.Fatal(err)
	}
}

func TestFailedAttachIsFatalOnlyWhenRequired(t *testing.T) {
	monitor := &failingTrafficMonitor{}
	allocator := newLinuxAllocator(nil, monitor, nil)
	request := request{VirtualMachineID: "vm-1", UserID: 1001}

	if err := allocator.convergeTrafficMonitoring(true, false, request); err != nil {
		t.Fatalf("a non-required attach failure should not fail the request: %v", err)
	}
	if err := allocator.convergeTrafficMonitoring(true, true, request); err == nil {
		t.Fatal("a required attach failure should fail the request")
	}
}

// failingTrafficMonitor always fails to attach.
type failingTrafficMonitor struct{}

func (*failingTrafficMonitor) Attach(traffic.AttachmentRequest) error {
	return errors.New("attach failed")
}

func (*failingTrafficMonitor) Detach(string) error {
	return nil
}

func TestFirewallAuditRunsForChangesAndAtItsInterval(t *testing.T) {
	now := time.Date(2026, time.September, 17, 12, 0, 0, 0, time.UTC)
	allocator := &LinuxAllocator{now: func() time.Time { return now }}
	disabled, err := firewallFingerprint(vm.FirewallConfiguration{})
	if err != nil {
		t.Fatal(err)
	}
	enabled, err := firewallFingerprint(vm.FirewallConfiguration{Enabled: true})
	if err != nil {
		t.Fatal(err)
	}

	if !allocator.isFirewallAuditDue("vm-1", disabled, false) {
		t.Fatal("first firewall inspection was skipped")
	}
	allocator.recordFirewallAudit("vm-1", disabled)
	if allocator.isFirewallAuditDue("vm-1", disabled, false) {
		t.Fatal("unchanged firewall was inspected again immediately")
	}
	if !allocator.isFirewallAuditDue("vm-1", enabled, false) {
		t.Fatal("changed firewall was not inspected")
	}

	now = now.Add(firewallAuditInterval)
	if !allocator.isFirewallAuditDue("vm-1", disabled, false) {
		t.Fatal("periodic firewall audit was skipped")
	}
}

func TestFirewallAuditCanBeForcedAndForgotten(t *testing.T) {
	allocator := newLinuxAllocator(nil, nil, nil)
	fingerprint, err := firewallFingerprint(vm.FirewallConfiguration{})
	if err != nil {
		t.Fatal(err)
	}
	allocator.recordFirewallAudit("vm-1", fingerprint)

	if !allocator.isFirewallAuditDue("vm-1", fingerprint, true) {
		t.Fatal("forced firewall inspection was skipped")
	}
	allocator.forgetFirewallAudit("vm-1")
	if !allocator.isFirewallAuditDue("vm-1", fingerprint, false) {
		t.Fatal("forgotten firewall inspection was skipped")
	}
}

func TestDisabledFirewallIgnoresRetainedRuleChanges(t *testing.T) {
	first, err := firewallFingerprint(vm.FirewallConfiguration{})
	if err != nil {
		t.Fatal(err)
	}
	second, err := firewallFingerprint(vm.FirewallConfiguration{
		Inbound: []vm.FirewallRule{{
			Protocol: vm.FirewallProtocolTCP,
			Ports:    "22",
			CIDRs:    []string{"203.0.113.0/24"},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}

	if first != second {
		t.Fatal("retained rules changed the effective disabled firewall")
	}
}

func TestMeshRegistrationIsSkippedWithoutAMesh(t *testing.T) {
	allocator := NewLinuxAllocator(nil, nil, nil)
	if err := allocator.addMeshRegistration(context.Background(), request{VirtualMachineID: "vm-1", UserID: 100000, NetworkConfiguration: vm.NetworkConfiguration{WireGuardMeshIPv6: "fdaa:1:0:1::1"}}); err != nil {
		t.Fatal(err)
	}
}

type fakeMesh struct {
	synced    []request
	removed   []string
	removeErr error
}

func (mesh *fakeMesh) syncVM(_ context.Context, request request) error {
	mesh.synced = append(mesh.synced, request)
	return nil
}

func (mesh *fakeMesh) removeVM(_ context.Context, address, interfaceName string) error {
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
	if err := allocator.addMeshRegistration(context.Background(), request{VirtualMachineID: "vm-1", UserID: 100000}); err != nil {
		t.Fatal(err)
	}
	if len(mesh.removed) != 0 || len(mesh.synced) != 0 {
		t.Errorf("mesh calls = %v and %v, want none", mesh.synced, mesh.removed)
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
