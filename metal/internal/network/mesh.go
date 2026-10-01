package network

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"strconv"
	"strings"

	platform "github.com/frappe/atlas/metal/internal/platform"
)

// meshMTU is the VM interface MTU required by Atlas WG Mesh. It leaves room for
// the outer IPv6 header inside the WireGuard MTU.
const meshMTU = 1380

// meshGatewayAddress is the link-local address the guest routes the mesh through.
const meshGatewayAddress = "fe80::1"

// meshPrefix is the Atlas mesh address block.
const meshPrefix = "fdaa::/16"

// gatewayTable holds the namespace route of a gateway VM. The host sends the
// traffic a gateway carries into its namespace, and the main table would send
// a destination outside the mesh straight back to the host.
const gatewayTable = "100"

// MeshConfig identifies the Atlas WG Mesh CLI and the host interfaces it uses.
// UplinkName must name the shared VLAN interface that carries Atlas NDP, never
// its parent, because the NDP hook and the proxy NDP entries attach to it.
type MeshConfig struct {
	CommandPath   string
	UplinkName    string
	WireGuardName string
	// WireGuardStatePath holds the managed WireGuard peer state that the mesh reads.
	WireGuardStatePath string
	// ControllerAddress is the Atlas mesh address on wg0.
	ControllerAddress string
}

// Mesh registers virtual machine addresses with the Atlas WG Mesh CLI.
type Mesh struct {
	commandPath        string
	uplinkName         string
	wireGuardName      string
	wireGuardStatePath string
	controllerAddress  string
}

// NewMesh returns a mesh registrar for one host.
func NewMesh(configuration MeshConfig) (*Mesh, error) {
	if configuration.CommandPath == "" {
		return nil, errors.New("Atlas WG Mesh command path is required")
	}
	if configuration.WireGuardName == "" {
		return nil, errors.New("WireGuard interface name is required")
	}
	if configuration.UplinkName == "" {
		return nil, errors.New("Atlas WG Mesh uplink interface name is required")
	}
	if configuration.WireGuardStatePath == "" {
		return nil, errors.New("Atlas WG Mesh peer state path is required")
	}
	if _, err := exec.LookPath(configuration.CommandPath); err != nil {
		return nil, fmt.Errorf("Atlas WG Mesh CLI %s: %w", configuration.CommandPath, err)
	}

	return &Mesh{
		commandPath:        configuration.CommandPath,
		uplinkName:         configuration.UplinkName,
		wireGuardName:      configuration.WireGuardName,
		wireGuardStatePath: configuration.WireGuardStatePath,
		controllerAddress:  configuration.ControllerAddress,
	}, nil
}

// Check the mesh registrar interface at compile time.
var _ meshRegistrar = (*Mesh)(nil)

// SyncPeerState replaces the BPF peer map and its NDP transport mode.
func (mesh *Mesh) SyncPeerState(ctx context.Context, unicast bool) error {
	arguments := []string{"peers", "sync", mesh.wireGuardStatePath}
	if unicast {
		arguments = append(arguments, "--unicast")
	}
	return platform.Run(ctx, mesh.commandPath, arguments...)
}

// PrivateNetworkMAC returns the MAC address of the mesh uplink.
// It reads one sysfs file, because every sync calls it and an interface lookup lists every link.
func (mesh *Mesh) PrivateNetworkMAC() (string, error) {
	contents, err := os.ReadFile("/sys/class/net/" + mesh.uplinkName + "/address")
	if err != nil {
		return "", fmt.Errorf("read uplink %s: %w", mesh.uplinkName, err)
	}
	mac, err := net.ParseMAC(strings.TrimSpace(string(contents)))
	if err != nil || len(mac) != 6 {
		return "", fmt.Errorf("uplink %s has no Ethernet MAC address", mesh.uplinkName)
	}
	return mac.String(), nil
}

// EnsureHost applies the host configuration and refreshes its BPF programs.
func (mesh *Mesh) EnsureHost(ctx context.Context) error {
	return platform.Run(ctx, mesh.commandPath, "configure",
		"--uplink", mesh.uplinkName, "--wireguard", mesh.wireGuardName, "--controller", mesh.controllerAddress)
}

// removeVM unregisters one VM address. An address this host does not own is not an error.
func (mesh *Mesh) removeVM(ctx context.Context, address, interfaceName string) error {
	return platform.Run(ctx, mesh.commandPath, "vm", "remove", "--interface", interfaceName, "--address", address)
}

// ApplyPrivilegedAddresses replaces the complete privileged VM whitelist.
func (mesh *Mesh) ApplyPrivilegedAddresses(ctx context.Context, desired []string) error {
	arguments := []string{"privileged-vm", "clear"}
	if len(desired) > 0 {
		arguments = append([]string{"privileged-vm", "replace"}, desired...)
	}
	return platform.Run(ctx, mesh.commandPath, arguments...)
}

// meshNamespaceSteps routes mesh traffic through the namespace. The guest
// owns its mesh address, and the proxy NDP entry answers for it from
// creation, before the guest applies its own copy. A permanent tap0 neighbour
// reaches a stopped guest. A zero proxy_delay answers the host at once, because
// the mesh drops packets until the host learns the guest.
func meshNamespaceSteps(guestVirtualEthernet, address string) [][]string {
	return [][]string{
		{"sysctl", "-q", "-w", "net.ipv6.conf.all.forwarding=1"},
		{"sysctl", "-q", "-w", "net.ipv6.conf." + guestVirtualEthernet + ".proxy_ndp=1"},
		{"sysctl", "-q", "-w", "net.ipv6.neigh." + guestVirtualEthernet + ".proxy_delay=0"},
		{"ip", "link", "set", guestVirtualEthernet, "mtu", strconv.Itoa(meshMTU)},
		{"ip", "-6", "addr", "replace", meshGatewayAddress + "/64", "dev", tapName, "nodad"},
		{"ip", "-6", "route", "replace", address + "/128", "dev", tapName},
		{"ip", "-6", "neigh", "replace", address, "lladdr", guestMACAddress, "dev", tapName, "nud", "permanent"},
		{"ip", "-6", "route", "replace", meshPrefix, "via", meshGatewayAddress, "dev", guestVirtualEthernet},
		{"ip", "-6", "neigh", "replace", "proxy", address, "dev", guestVirtualEthernet},
	}
}

// syncVM applies the host routes first and registers the complete VM state last.
func (mesh *Mesh) syncVM(ctx context.Context, request request) error {
	if request.WireGuardMeshIPv6 == "" {
		return nil
	}
	hostVirtualEthernet, _ := virtualEthernetNames(request.UserID)
	if err := mesh.convergeOwnedPrefixRoutes(ctx, request); err != nil {
		return err
	}
	if err := mesh.convergeGatewayRoute(ctx, request); err != nil {
		return err
	}

	arguments := []string{
		"vm", "sync", "--interface", hostVirtualEthernet,
		"--address", request.WireGuardMeshIPv6, "--mtu", strconv.Itoa(meshMTU),
	}
	if request.IsNetworkGateway {
		arguments = append(arguments, "--gateway")
	}
	if request.PublicIPv6 != "" {
		arguments = append(arguments, "--prefix", request.PublicIPv6)
	}
	for _, route := range request.RoutesViaGateway() {
		arguments = append(arguments, "--route", route.Destination+"="+route.Via)
	}
	return platform.Run(ctx, mesh.commandPath, arguments...)
}

// convergeGatewayRoute sends host traffic in a gateway namespace to the guest.
func (mesh *Mesh) convergeGatewayRoute(ctx context.Context, request request) error {
	_, guestVirtualEthernet := virtualEthernetNames(request.UserID)
	namespace := namespaceName(request.VirtualMachineID)
	rules, err := platform.RunInNetworkNamespace(ctx, namespace, "ip", "-6", "rule", "show", "iif", guestVirtualEthernet)
	if err != nil {
		return fmt.Errorf("read the gateway rule of %s: %w", request.VirtualMachineID, err)
	}

	hasRule := strings.Contains(rules, "lookup "+gatewayTable)
	for _, step := range gatewayRouteSteps(guestVirtualEthernet, request.WireGuardMeshIPv6, request.IsNetworkGateway, hasRule) {
		if _, err := platform.RunInNetworkNamespace(ctx, namespace, step[0], step[1:]...); err != nil {
			return fmt.Errorf("change the gateway route of %s: %w", request.VirtualMachineID, err)
		}
	}
	return nil
}

// gatewayRouteSteps adds or removes the gateway rule and its route.
func gatewayRouteSteps(guestVirtualEthernet, address string, isGateway, hasRule bool) [][]string {
	switch {
	case isGateway && hasRule:
		return [][]string{{"ip", "-6", "route", "replace", "default", "via", address, "dev", tapName, "table", gatewayTable}}
	case isGateway:
		return [][]string{
			{"ip", "-6", "route", "replace", "default", "via", address, "dev", tapName, "table", gatewayTable},
			{"ip", "-6", "rule", "add", "iif", guestVirtualEthernet, "lookup", gatewayTable},
		}
	case hasRule:
		return [][]string{
			{"ip", "-6", "rule", "del", "iif", guestVirtualEthernet, "lookup", gatewayTable},
			{"ip", "-6", "route", "flush", "table", gatewayTable},
		}
	}
	return nil
}

func (mesh *Mesh) convergeOwnedPrefixRoutes(ctx context.Context, request request) error {
	hostVirtualEthernet, _ := virtualEthernetNames(request.UserID)
	present, err := platform.Output(ctx, "ip", "-6", "route", "show", "dev", hostVirtualEthernet)
	if err != nil {
		return fmt.Errorf("read the routes of %s: %w", hostVirtualEthernet, err)
	}

	for line := range strings.SplitSeq(present, "\n") {
		fields := strings.Fields(line)
		if len(fields) == 0 || fieldAfter(fields, "via") != request.WireGuardMeshIPv6 || fields[0] == request.PublicIPv6 {
			continue
		}
		if err := changeHostPrefixRoute(ctx, request, "del", fields[0]); err != nil {
			return err
		}
	}
	if request.PublicIPv6 != "" {
		if err := changeHostPrefixRoute(ctx, request, "replace", request.PublicIPv6); err != nil {
			return err
		}
	}
	return convergeGuestPrefixRoute(ctx, request)
}

// changeHostPrefixRoute uses onlink because vm sync adds the VM address route later.
func changeHostPrefixRoute(ctx context.Context, request request, action, prefix string) error {
	hostVirtualEthernet, _ := virtualEthernetNames(request.UserID)
	if err := platform.Run(ctx, "ip", "-6", "route", action, prefix,
		"via", request.WireGuardMeshIPv6, "dev", hostVirtualEthernet, "onlink"); err != nil {
		return fmt.Errorf("%s the route of %s: %w", action, prefix, err)
	}
	return nil
}

// convergeGuestPrefixRoute sends a routed block from the namespace to the guest,
// which owns its addresses. A /128 has no guest route: the host maps it to the
// mesh address, and a namespace route would return the guest's traffic to its
// own public address back to the guest before it reaches the host.
func convergeGuestPrefixRoute(ctx context.Context, request request) error {
	namespace := namespaceName(request.VirtualMachineID)
	wanted := ""
	if request.PublicIPv6 != "" && !request.HasPublicIPv6Address() {
		wanted = request.PublicIPv6
	}

	output, err := platform.RunInNetworkNamespace(ctx, namespace, "ip", "-6", "route", "show",
		"via", request.WireGuardMeshIPv6, "dev", tapName)
	if err != nil {
		return fmt.Errorf("read the guest prefix routes of %s: %w", request.VirtualMachineID, err)
	}
	for _, prefix := range parseNamespaceRoutes(output, routeFamilies(request.UserID)[1]) {
		if prefix == wanted {
			continue
		}
		if _, err := platform.RunInNetworkNamespace(ctx, namespace, "ip", "-6", "route", "del", prefix,
			"via", request.WireGuardMeshIPv6, "dev", tapName); err != nil {
			return fmt.Errorf("remove the guest route of %s: %w", prefix, err)
		}
	}
	if wanted == "" {
		return nil
	}
	if _, err := platform.RunInNetworkNamespace(ctx, namespace, "ip", "-6", "route", "replace", wanted,
		"via", request.WireGuardMeshIPv6, "dev", tapName); err != nil {
		return fmt.Errorf("add the guest route of %s: %w", wanted, err)
	}
	return nil
}

func fieldAfter(fields []string, key string) string {
	for index, field := range fields {
		if field == key && index+1 < len(fields) {
			return fields[index+1]
		}
	}
	return ""
}
