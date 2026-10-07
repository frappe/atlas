package network

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	traffic "github.com/frappe/atlas/metal/internal/network/traffic"
	platform "github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/vm"
)

// VMs reuse private addresses across isolated namespaces. The fixed guest MAC
// encodes 172.16.0.2 and keeps a stopped guest reachable.
const (
	tapName               = "tap0"
	gatewayIPAddress      = "172.16.0.1"
	guestIPAddress        = "172.16.0.2"
	networkPrefixLength   = 24
	guestMACAddress       = "06:00:ac:10:00:02"
	firewallAuditInterval = time.Minute
)

type firewallAudit struct {
	fingerprint string
	inspectedAt time.Time
}

// meshRegistrar registers and removes the complete mesh state of one VM.
type meshRegistrar interface {
	syncVM(ctx context.Context, request request) error
	removeVM(ctx context.Context, address, interfaceName string) error
}

// trafficMonitor attaches packet monitoring to VM TAP devices.
type trafficMonitor interface {
	Attach(traffic.AttachmentRequest) error
	Detach(string) error
}

// LinuxAllocator creates Linux network resources for virtual machines.
type LinuxAllocator struct {
	mesh           meshRegistrar
	uplinkName     string
	trafficMonitor trafficMonitor
	firewallMutex  sync.Mutex
	firewallAudits map[string]firewallAudit
	now            func() time.Time
	logger         *slog.Logger
}

// NewLinuxAllocator returns a Linux network allocator.
func NewLinuxAllocator(mesh *Mesh, monitor *traffic.Monitor, logger *slog.Logger) *LinuxAllocator {
	var registrar meshRegistrar
	if mesh != nil {
		registrar = mesh
	}
	var trafficMonitor trafficMonitor
	if monitor != nil {
		trafficMonitor = monitor
	}
	allocator := newLinuxAllocator(registrar, trafficMonitor, logger)
	if mesh != nil {
		allocator.uplinkName = mesh.uplinkName
	}
	return allocator
}

// newLinuxAllocator returns an allocator with test dependencies.
func newLinuxAllocator(mesh meshRegistrar, trafficMonitor trafficMonitor, logger *slog.Logger) *LinuxAllocator {
	if logger == nil {
		logger = slog.Default()
	}
	return &LinuxAllocator{
		mesh:           mesh,
		trafficMonitor: trafficMonitor,
		firewallAudits: make(map[string]firewallAudit),
		now:            time.Now,
		logger:         logger,
	}
}

// Ensure converges all host network resources to the requested state.
func (allocator *LinuxAllocator) Ensure(ctx context.Context, desired vm.NetworkRequest) (vm.NetworkInterface, error) {
	request := request{
		NetworkConfiguration: desired.Configuration,
		VirtualMachineID:     desired.VirtualMachineID,
		UserID:               desired.UserID,
		GroupID:              desired.GroupID,
	}
	namespaceCreated, err := ensureNamespace(ctx, request.VirtualMachineID)
	if err != nil {
		return vm.NetworkInterface{}, err
	}
	if err := allocator.converge(ctx, request, namespaceCreated); err != nil {
		return vm.NetworkInterface{}, err
	}
	if err := allocator.convergeTrafficMonitoring(desired.TrackTraffic, desired.FailOnTrafficMonitorAttachError, request); err != nil {
		return vm.NetworkInterface{}, err
	}

	return allocator.interfaceFor(request.VirtualMachineID), nil
}

// Release removes a VM network and its mesh registration. It unregisters mesh
// before deleting the namespace, which also deletes its veth pair.
func (allocator *LinuxAllocator) Release(ctx context.Context, request ReleaseRequest) error {
	virtualMachineID := request.VirtualMachineID
	allocator.forgetFirewallAudit(virtualMachineID)
	// Release TCX links before tap0 is removed.
	var trafficError error
	if allocator.trafficMonitor != nil {
		trafficError = allocator.trafficMonitor.Detach(virtualMachineID)
	}
	meshError := allocator.removeMeshRegistration(ctx, request.UserID, request.WireGuardMeshIPv6)
	rulesError := errors.Join(removePublicIPv4Rules(ctx, virtualMachineID), removePublicIPv6Rules(ctx, virtualMachineID))
	if allocator.uplinkName != "" {
		rulesError = errors.Join(rulesError, releasePublicIPv4Claims(ctx, request.UserID, allocator.uplinkName, ""))
	}

	exists, err := networkNamespaceExists(ctx, virtualMachineID)
	if err != nil {
		return errors.Join(trafficError, meshError, rulesError, err)
	}
	if !exists {
		return errors.Join(trafficError, meshError, rulesError)
	}
	namespaceRulesError := removePublicIPv4NamespaceRules(ctx, virtualMachineID)

	namespaceError := platform.Run(ctx, "ip", "netns", "del", namespaceName(virtualMachineID))
	return errors.Join(trafficError, meshError, rulesError, namespaceRulesError, namespaceError)
}

func (allocator *LinuxAllocator) convergeTrafficMonitoring(enabled, failOnAttachError bool, request request) error {
	if allocator.trafficMonitor == nil {
		return nil
	}
	if !enabled {
		if err := allocator.trafficMonitor.Detach(request.VirtualMachineID); err != nil {
			return fmt.Errorf("detach traffic monitor: %w", err)
		}
		return nil
	}

	err := allocator.trafficMonitor.Attach(traffic.AttachmentRequest{
		Target: traffic.Target{
			VirtualMachineID: request.VirtualMachineID,
			UserID:           request.UserID,
		},
		NamespacePath: namespacePath(request.VirtualMachineID),
		InterfaceName: tapName,
	})
	if err == nil {
		return nil
	}
	if failOnAttachError {
		return fmt.Errorf("attach traffic monitor: %w", err)
	}
	allocator.logger.Warn("traffic monitor attach failed, continuing without traffic metrics",
		"virtual_machine_id", request.VirtualMachineID, "error", err)
	return nil
}

// ensureNamespace creates the VM network namespace when it is absent.
func ensureNamespace(ctx context.Context, virtualMachineID string) (bool, error) {
	exists, err := networkNamespaceExists(ctx, virtualMachineID)
	if err != nil || exists {
		return false, err
	}

	if err := platform.Run(ctx, "ip", "netns", "add", namespaceName(virtualMachineID)); err != nil {
		return false, err
	}
	return true, nil
}

// converge removes dropped resources before adding requested ones, so a route
// change never leaves both network shapes in place.
func (allocator *LinuxAllocator) converge(ctx context.Context, request request, namespaceCreated bool) error {
	if request.PublicIPv4 != "" && !request.HasIPv4HostRoute() {
		return fmt.Errorf("public IPv4 requires an IPv4 route via %s", vm.RouteViaHost)
	}
	if request.PublicIPv6 != "" && !request.HasIPv6HostRoute() {
		return fmt.Errorf("public IPv6 requires an IPv6 route via %s", vm.RouteViaHost)
	}

	if err := ensureNamespaceBase(ctx, request); err != nil {
		return err
	}
	if err := allocator.convergeFirewall(ctx, request, namespaceCreated); err != nil {
		return err
	}
	if err := allocator.removeUnwanted(ctx, request); err != nil {
		return err
	}
	if err := allocator.addWanted(ctx, request); err != nil {
		return err
	}

	return configureTrafficControl(ctx, request.trafficControl())
}

func (allocator *LinuxAllocator) convergeFirewall(
	ctx context.Context,
	request request,
	force bool,
) error {
	fingerprint, err := firewallFingerprint(request.Firewall)
	if err != nil {
		return err
	}
	if !allocator.isFirewallAuditDue(request.VirtualMachineID, fingerprint, force) {
		return nil
	}
	if err := ensureFirewall(ctx, namespaceName(request.VirtualMachineID), request.Firewall); err != nil {
		return err
	}
	allocator.recordFirewallAudit(request.VirtualMachineID, fingerprint)
	return nil
}

func firewallFingerprint(configuration vm.FirewallConfiguration) (string, error) {
	ipv4Table, err := renderFirewallTable(configuration, false)
	if err != nil {
		return "", err
	}
	ipv6Table, err := renderFirewallTable(configuration, true)
	if err != nil {
		return "", err
	}

	return ipv4Table + "\x00" + ipv6Table, nil
}

func (allocator *LinuxAllocator) isFirewallAuditDue(
	virtualMachineID string,
	fingerprint string,
	force bool,
) bool {
	if force {
		return true
	}
	now := allocator.currentTime()
	allocator.firewallMutex.Lock()
	defer allocator.firewallMutex.Unlock()

	audit, found := allocator.firewallAudits[virtualMachineID]
	return !found || audit.fingerprint != fingerprint || now.Sub(audit.inspectedAt) >= firewallAuditInterval
}

func (allocator *LinuxAllocator) recordFirewallAudit(virtualMachineID, fingerprint string) {
	allocator.firewallMutex.Lock()
	defer allocator.firewallMutex.Unlock()
	if allocator.firewallAudits == nil {
		allocator.firewallAudits = make(map[string]firewallAudit)
	}
	allocator.firewallAudits[virtualMachineID] = firewallAudit{
		fingerprint: fingerprint,
		inspectedAt: allocator.currentTime(),
	}
}

func (allocator *LinuxAllocator) forgetFirewallAudit(virtualMachineID string) {
	allocator.firewallMutex.Lock()
	defer allocator.firewallMutex.Unlock()
	delete(allocator.firewallAudits, virtualMachineID)
}

func (allocator *LinuxAllocator) currentTime() time.Time {
	if allocator.now != nil {
		return allocator.now()
	}
	return time.Now()
}

// removeUnwanted tears down what this request no longer asks for.
func (allocator *LinuxAllocator) removeUnwanted(ctx context.Context, request request) error {
	if allocator.uplinkName != "" {
		if err := releasePublicIPv4Claims(ctx, request.UserID, allocator.uplinkName, request.PublicIPv4); err != nil {
			return err
		}
	}
	if request.PublicIPv4 == "" {
		if err := removePublicIPv4Rules(ctx, request.VirtualMachineID); err != nil {
			return err
		}
		if err := removePublicIPv4NamespaceRules(ctx, request.VirtualMachineID); err != nil {
			return err
		}
	}
	if !request.HasPublicIPv6Address() {
		if err := removePublicIPv6Rules(ctx, request.VirtualMachineID); err != nil {
			return err
		}
	}
	if !request.HasNetworkAttachment() {
		return allocator.removeMeshRegistration(ctx, request.UserID, request.WireGuardMeshIPv6)
	}
	return nil
}

// addWanted builds requested resources in dependency order. Mesh registration is
// last, so its first packet finds a complete path.
func (allocator *LinuxAllocator) addWanted(ctx context.Context, request request) error {
	if err := setVirtualEthernet(ctx, request.VirtualMachineID, request.UserID, request.HasNetworkAttachment()); err != nil {
		return err
	}
	if !request.HasNetworkAttachment() {
		return nil
	}

	if err := ensureMaximumSegmentSizeClamp(ctx, request.VirtualMachineID, request.UserID); err != nil {
		return err
	}
	if err := convergeNamespaceRoutes(ctx, request); err != nil {
		return err
	}
	if request.PublicIPv4 != "" {
		if err := ensurePublicIPv4(ctx, request.VirtualMachineID, request.UserID, request.PublicIPv4); err != nil {
			return err
		}
		if allocator.uplinkName != "" {
			if err := claimPublicIPv4(ctx, request.UserID, allocator.uplinkName, request.PublicIPv4); err != nil {
				return err
			}
		}
	}
	if request.HasPublicIPv6Address() {
		if err := ensurePublicIPv6(ctx, request.VirtualMachineID, request.PublicIPv6, request.WireGuardMeshIPv6); err != nil {
			return err
		}
	}
	return allocator.addMeshRegistration(ctx, request)
}

// interfaceFor returns the values a runtime needs to attach one VM.
func (allocator *LinuxAllocator) interfaceFor(virtualMachineID string) Interface {
	return Interface{
		NetworkNamespacePath: namespacePath(virtualMachineID),
		TapName:              tapName,
		MACAddress:           guestMACAddress,
		GuestIPAddress:       guestIPAddress,
		GatewayIPAddress:     gatewayIPAddress,
	}
}

// addMeshRegistration routes the guest mesh address through the namespace and
// registers it with Atlas WG Mesh.
func (allocator *LinuxAllocator) addMeshRegistration(ctx context.Context, request request) error {
	address := request.WireGuardMeshIPv6
	if allocator.mesh == nil || address == "" {
		return nil
	}

	_, guestVirtualEthernet := virtualEthernetNames(request.UserID)
	if err := runNetworkNamespaceSteps(ctx, namespaceName(request.VirtualMachineID), meshNamespaceSteps(guestVirtualEthernet, address)); err != nil {
		return fmt.Errorf("route mesh address %s: %w", address, err)
	}
	if err := allocator.mesh.syncVM(ctx, request); err != nil {
		return fmt.Errorf("register mesh address %s: %w", address, err)
	}
	return nil
}

// removeMeshRegistration unregisters the guest mesh address.
func (allocator *LinuxAllocator) removeMeshRegistration(ctx context.Context, userID uint32, address string) error {
	if allocator.mesh == nil || address == "" {
		return nil
	}

	hostVirtualEthernet, _ := virtualEthernetNames(userID)
	if err := allocator.mesh.removeVM(ctx, address, hostVirtualEthernet); err != nil {
		return fmt.Errorf("unregister mesh address %s: %w", address, err)
	}
	return nil
}

var _ vm.Network = (*LinuxAllocator)(nil)
