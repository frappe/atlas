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

	"github.com/frappe/atlas/metal/internal/hostcmd"
)

// meshMTU is the VM interface MTU that Atlas WG Mesh needs. A mesh packet gains
// a 40-byte outer IPv6 header and must still fit the 1420-byte WireGuard MTU.
const meshMTU = 1380

const meshGatewayAddress = "fe80::1"

const meshPrefix = "fdaa::/16"

// MeshConfig identifies the Atlas WG Mesh CLI and the host interfaces it uses.
// UplinkName must name the interface that carries discovery, never its parent.
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

// EnsureHost configures Atlas WG Mesh when this host has no configuration, and
// rejects a configuration that discovers on another interface.
func (mesh *Mesh) EnsureHost(ctx context.Context) error {
	status, statusError := hostcmd.Output(ctx, mesh.commandPath, "status")
	if statusError == nil {
		return mesh.verifyDiscoveryInterface(status)
	}

	err := hostcmd.Run(ctx, mesh.commandPath, "configure",
		"--uplink", mesh.uplinkName, "--wireguard", mesh.wireGuardName)
	if err != nil {
		return errors.Join(fmt.Errorf("read Atlas WG Mesh status: %w", statusError), err)
	}
	return nil
}

// verifyDiscoveryInterface rejects a host that discovers on another interface.
// The uplink hook consumes the discovery traffic of every VLAN under it.
func (mesh *Mesh) verifyDiscoveryInterface(status string) error {
	name := discoveryInterface(status)
	if name == "" {
		return fmt.Errorf("Atlas WG Mesh status names no discovery interface")
	}
	if name != mesh.uplinkName {
		return fmt.Errorf(
			"Atlas WG Mesh discovers on %s and not %s: reset the host to change the interface",
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
	return hostcmd.Run(ctx, mesh.commandPath, "vm", "add",
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
	return hostcmd.Run(ctx, mesh.commandPath, "vm", "remove", "--interface", interfaceName, "--address", address)
}

// ApplyPrivilegedAddresses replaces the privileged VM whitelist with the
// complete desired set. Only these tenant-0 addresses cross tenants.
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

func (mesh *Mesh) setPrivileged(ctx context.Context, action, address string) error {
	if err := hostcmd.Run(ctx, mesh.commandPath, "privileged-vm", action, "--address", address); err != nil {
		return fmt.Errorf("%s privileged mesh address %s: %w", action, address, err)
	}
	return nil
}

// privilegedAddresses returns the whitelist that this host holds now.
func (mesh *Mesh) privilegedAddresses(ctx context.Context) (map[string]struct{}, error) {
	output, err := hostcmd.Output(ctx, mesh.commandPath, "privileged-vm", "list", "--json")
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

// parseMeshAddresses normalises the desired set, so a differently written
// address does not read as a change.
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

	output, err := hostcmd.Output(ctx, mesh.commandPath, "vm", "list", "--json")
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

// meshNamespaceSteps makes the namespace an IPv6 router for the guest mesh
// address. The host route from Atlas WG Mesh is on-link on the host veth, so the
// namespace answers neighbour solicitations for the guest with proxy NDP.
func meshNamespaceSteps(namespace, guestVirtualEthernet, address string) [][]string {
	return [][]string{
		{"ip", "netns", "exec", namespace, "sysctl", "-q", "-w", "net.ipv6.conf.all.forwarding=1"},
		{"ip", "netns", "exec", namespace, "sysctl", "-q", "-w", "net.ipv6.conf." + guestVirtualEthernet + ".proxy_ndp=1"},
		{"ip", "-n", namespace, "link", "set", guestVirtualEthernet, "mtu", strconv.Itoa(meshMTU)},
		{"ip", "-n", namespace, "-6", "addr", "replace", meshGatewayAddress + "/64", "dev", tapName, "nodad"},
		{"ip", "-n", namespace, "-6", "route", "replace", address + "/128", "dev", tapName},
		{"ip", "-n", namespace, "-6", "route", "replace", meshPrefix, "via", meshGatewayAddress, "dev", guestVirtualEthernet},
		{"ip", "-n", namespace, "-6", "neigh", "replace", "proxy", address, "dev", guestVirtualEthernet},
	}
}
