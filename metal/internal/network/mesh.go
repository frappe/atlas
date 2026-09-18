package network

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
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

// MeshConfig identifies the Atlas WG Mesh CLI and the host interfaces it uses.
// UplinkName must name the shared VLAN interface that carries Atlas NDP, never
// its parent, because the NDP hook and the proxy NDP entries attach to it.
type MeshConfig struct {
	CommandPath   string
	UplinkName    string
	WireGuardName string
}

// Mesh registers virtual machine addresses with the Atlas WG Mesh CLI.
type Mesh struct {
	commandPath   string
	uplinkName    string
	wireGuardName string
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
	if _, err := exec.LookPath(configuration.CommandPath); err != nil {
		return nil, fmt.Errorf("Atlas WG Mesh CLI %s: %w", configuration.CommandPath, err)
	}

	return &Mesh{
		commandPath:   configuration.CommandPath,
		uplinkName:    configuration.UplinkName,
		wireGuardName: configuration.WireGuardName,
	}, nil
}

// Check the mesh registrar interface at compile time.
var _ meshRegistrar = (*Mesh)(nil)

// EnsureHost configures an unconfigured host and verifies its discovery
// interface.
func (mesh *Mesh) EnsureHost(ctx context.Context) error {
	status, statusError := platform.Output(ctx, mesh.commandPath, "status")
	if statusError == nil {
		return mesh.verifyDiscoveryInterface(status)
	}

	err := platform.Run(ctx, mesh.commandPath, "configure",
		"--uplink", mesh.uplinkName, "--wireguard", mesh.wireGuardName)
	if err != nil {
		return errors.Join(fmt.Errorf("read Atlas WG Mesh status: %w", statusError), err)
	}
	return nil
}

// verifyDiscoveryInterface rejects a host that carries Atlas NDP on another
// interface. The CLI reads the interface from the pinned configuration.
func (mesh *Mesh) verifyDiscoveryInterface(status string) error {
	name := discoveryInterface(status)
	if name == "" {
		return fmt.Errorf("Atlas WG Mesh status names no discovery interface")
	}
	if name != mesh.uplinkName {
		return fmt.Errorf(
			"Atlas WG Mesh carries NDP on %s and not %s: reset the host to change the interface",
			name, mesh.uplinkName,
		)
	}
	return nil
}

// discoveryInterface reads NAME from "discovery interface: NAME (multicast)".
func discoveryInterface(status string) string {
	for line := range strings.SplitSeq(status, "\n") {
		name, found := strings.CutPrefix(strings.TrimSpace(line), "discovery interface: ")
		if !found {
			continue
		}
		if fields := strings.Fields(name); len(fields) > 0 {
			return fields[0]
		}
	}
	return ""
}

// Add registers one VM address on a host interface and announces its location.
func (mesh *Mesh) Add(ctx context.Context, address, interfaceName string) error {
	return platform.Run(ctx, mesh.commandPath, "vm", "add",
		"--interface", interfaceName,
		"--address", address,
		"--mtu", strconv.Itoa(meshMTU),
	)
}

// Remove unregisters one VM address. An address this host does not own is not an error.
func (mesh *Mesh) Remove(ctx context.Context, address, interfaceName string) error {
	registered, err := mesh.IsRegistered(ctx, address)
	if err != nil || !registered {
		return err
	}
	return platform.Run(ctx, mesh.commandPath, "vm", "remove", "--interface", interfaceName, "--address", address)
}

// ApplyPrivilegedAddresses replaces the complete privileged VM whitelist.
func (mesh *Mesh) ApplyPrivilegedAddresses(ctx context.Context, desired []string) error {
	wanted, err := parseMeshAddresses(desired)
	if err != nil {
		return err
	}
	current, err := mesh.privilegedAddresses(ctx)
	if err != nil {
		return err
	}

	var applyErrors []error
	for address := range current {
		if _, keep := wanted[address]; !keep {
			applyErrors = append(applyErrors, mesh.setPrivileged(ctx, "remove", address))
		}
	}
	for address := range wanted {
		if _, present := current[address]; !present {
			applyErrors = append(applyErrors, mesh.setPrivileged(ctx, "add", address))
		}
	}
	return errors.Join(applyErrors...)
}

// setPrivileged adds or removes one address from the privileged whitelist.
func (mesh *Mesh) setPrivileged(ctx context.Context, action, address string) error {
	if err := platform.Run(ctx, mesh.commandPath, "privileged-vm", action, "--address", address); err != nil {
		return fmt.Errorf("%s privileged mesh address %s: %w", action, address, err)
	}
	return nil
}

// privilegedAddresses returns the whitelist that this host holds now.
func (mesh *Mesh) privilegedAddresses(ctx context.Context) (map[string]struct{}, error) {
	output, err := platform.Output(ctx, mesh.commandPath, "privileged-vm", "list", "--json")
	if err != nil {
		return nil, fmt.Errorf("list privileged mesh addresses: %w", err)
	}

	var entries []struct {
		Address string `json:"address"`
	}
	if err := json.Unmarshal([]byte(output), &entries); err != nil {
		return nil, fmt.Errorf("decode privileged mesh addresses: %w", err)
	}

	addresses := make(map[string]struct{}, len(entries))
	for _, entry := range entries {
		address, err := netip.ParseAddr(entry.Address)
		if err != nil {
			return nil, fmt.Errorf("parse privileged mesh address %q: %w", entry.Address, err)
		}
		addresses[address.String()] = struct{}{}
	}
	return addresses, nil
}

// parseMeshAddresses normalises the desired address set.
func parseMeshAddresses(values []string) (map[string]struct{}, error) {
	addresses := make(map[string]struct{}, len(values))
	for _, value := range values {
		address, err := netip.ParseAddr(value)
		if err != nil {
			return nil, fmt.Errorf("parse privileged mesh address %q: %w", value, err)
		}
		addresses[address.String()] = struct{}{}
	}
	return addresses, nil
}

// IsRegistered reports whether this host owns one VM address.
func (mesh *Mesh) IsRegistered(ctx context.Context, address string) (bool, error) {
	wanted, err := netip.ParseAddr(address)
	if err != nil {
		return false, fmt.Errorf("parse mesh address %q: %w", address, err)
	}

	output, err := platform.Output(ctx, mesh.commandPath, "vm", "list", "--json")
	if err != nil {
		return false, fmt.Errorf("list Atlas WG Mesh VMs: %w", err)
	}

	var entries []struct {
		Address string `json:"address"`
	}
	if err := json.Unmarshal([]byte(output), &entries); err != nil {
		return false, fmt.Errorf("decode Atlas WG Mesh VM list: %w", err)
	}
	for _, entry := range entries {
		if registered, err := netip.ParseAddr(entry.Address); err == nil && registered == wanted {
			return true, nil
		}
	}
	return false, nil
}

// meshNamespaceSteps routes mesh traffic through the namespace. The mesh
// address is assigned to the veth inside the namespace, so the address is
// present from creation without depending on the guest applying its own copy.
// Proxy NDP handles the veth, and a permanent tap0 neighbour reaches a
// stopped guest.
func meshNamespaceSteps(guestVirtualEthernet, address string) [][]string {
	return [][]string{
		{"sysctl", "-q", "-w", "net.ipv6.conf.all.forwarding=1"},
		{"sysctl", "-q", "-w", "net.ipv6.conf." + guestVirtualEthernet + ".proxy_ndp=1"},
		{"ip", "link", "set", guestVirtualEthernet, "mtu", strconv.Itoa(meshMTU)},
		{"ip", "-6", "addr", "replace", meshGatewayAddress + "/64", "dev", tapName, "nodad"},
		{"ip", "-6", "addr", "replace", address + "/128", "dev", guestVirtualEthernet},
		{"ip", "-6", "route", "replace", address + "/128", "dev", tapName},
		{"ip", "-6", "neigh", "replace", address, "lladdr", guestMACAddress, "dev", tapName, "nud", "permanent"},
		{"ip", "-6", "route", "replace", meshPrefix, "via", meshGatewayAddress, "dev", guestVirtualEthernet},
		{"ip", "-6", "neigh", "replace", "proxy", address, "dev", guestVirtualEthernet},
	}
}
