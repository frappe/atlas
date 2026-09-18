package main

import (
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"
)

var addInterfaceName, addAddressText string

var addMTU uint32

var removeInterfaceName, removeAddressText string

var virtualMachineCommand = &cobra.Command{
	Use:   "vm",
	Short: "manage the VMs on this host",
	Args:  cobra.NoArgs,
	RunE:  showHelp,
}

var addVirtualMachineCommand = &cobra.Command{
	Use:   "add",
	Short: "register a local VM and announce its location",
	Args:  cobra.NoArgs,
	RunE: func(*cobra.Command, []string) error {
		return addVirtualMachine(addInterfaceName, addAddressText, addMTU)
	},
}

var removeVirtualMachineCommand = &cobra.Command{
	Use:   "remove",
	Short: "remove a local VM",
	Args:  cobra.NoArgs,
	RunE: func(*cobra.Command, []string) error {
		return removeVirtualMachine(removeInterfaceName, removeAddressText)
	},
}

func installHost(uplinkName, wireGuardName string) error {
	if _, err := os.Stat(filepath.Join(pinDirectory, "config")); err == nil {
		return errors.New("Atlas WG Mesh is already configured")
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}

	config, err := readHostConfig(uplinkName, wireGuardName)
	if err != nil {
		return err
	}
	if err := requireNeighbourKfunc(); err != nil {
		return err
	}

	collection, err := loadCollection(nil)
	if err != nil {
		return err
	}
	defer collection.Close()

	if err := mountBPFFileSystem(); err != nil {
		return err
	}

	if err := runCommand("sysctl", "-qw", "net.ipv6.conf.all.forwarding=1"); err != nil {
		return err
	}

	if err := runCommand("ip", "link", "set", uplinkName, "allmulticast", "on"); err != nil {
		return err
	}

	proxyNDPPath := "/proc/sys/net/ipv6/conf/" + uplinkName + "/proxy_ndp"
	if err := os.WriteFile(proxyNDPPath, []byte("1\n"), 0644); err != nil {
		return fmt.Errorf("enable proxy_ndp on %s: %w", uplinkName, err)
	}

	if err := setMeshRoute(uplinkName); err != nil {
		return err
	}

	if err := pinCollection(collection, config); err != nil {
		return rollbackInstall(err, uplinkName, wireGuardName)
	}

	if err := attachHook(uplinkName, ndpProgram, "ingress"); err != nil {
		return rollbackInstall(err, uplinkName, wireGuardName)
	}

	if err := attachHook(uplinkName, ndpProgram, "egress"); err != nil {
		return rollbackInstall(err, uplinkName, wireGuardName)
	}

	if err := attachHook(wireGuardName, wireguardProgram, "ingress"); err != nil {
		return rollbackInstall(err, uplinkName, wireGuardName)
	}

	if err := attachNUDHooks(); err != nil {
		return rollbackInstall(err, uplinkName, wireGuardName)
	}

	fmt.Printf("Atlas WG Mesh is installed on %s and %s\n", uplinkName, wireGuardName)
	return nil
}

// rollbackInstall removes the VLAN route, detaches the hooks on the uplink and
// the WireGuard interface, and removes the pinned BPF state. A hook that was
// not attached yet is not an error.
func rollbackInstall(cause error, uplinkName, wireGuardName string) error {
	if err := removeMeshRoute(uplinkName); err != nil {
		return errors.Join(cause, fmt.Errorf("remove VLAN route: %w", err))
	}

	if err := detachHook(uplinkName); err != nil {
		return errors.Join(cause, fmt.Errorf("detach %s hook: %w", uplinkName, err))
	}

	if err := detachHook(wireGuardName); err != nil {
		return errors.Join(cause, fmt.Errorf("detach %s hook: %w", wireGuardName, err))
	}

	if err := clearPinDirectory(); err != nil {
		return errors.Join(cause, fmt.Errorf("remove partial BPF state: %w", err))
	}

	return cause
}

func removeHost(force bool) error {
	unlock, err := lockVMState()
	if err != nil {
		return err
	}
	defer unlock()

	virtualMachines, err := localVirtualMachines()
	if err != nil {
		return err
	}
	count := len(virtualMachines)
	if count != 0 && !force {
		return fmt.Errorf("remove every VM first; %d local VM entries remain", count)
	}
	interfaces, err := configuredInterfaces()
	if err != nil {
		return err
	}
	hookInterfaces := []string{interfaces.uplinkName, interfaces.wireGuardName}
	if force {
		for _, virtualMachine := range virtualMachines {
			hookInterfaces = append(hookInterfaces, virtualMachine.interfaceName)
		}
	}
	detached := make(map[string]struct{})
	for _, name := range hookInterfaces {
		if _, alreadyDetached := detached[name]; alreadyDetached {
			continue
		}
		if name != "" {
			if err := detachHook(name); err != nil {
				return err
			}
			// A host that ran the unicast daemon also holds the
			// unicast filters on its uplink.
			if name == interfaces.uplinkName {
				detachUnicastHookWarning(name, "ingress")
				detachUnicastHookWarning(name, "egress")
			}
			detached[name] = struct{}{}
		}
	}
	if interfaces.uplinkName != "" {
		// Every VM entry is cleared with force, so no proxy entry may remain.
		if force {
			for _, virtualMachine := range virtualMachines {
				_ = removeProxyNeighbour(virtualMachine.address.String(), interfaces.uplinkName)
			}
		}
		if err := removeMeshRoute(interfaces.uplinkName); err != nil {
			return err
		}
	}
	if err := clearPinDirectory(); err != nil {
		return err
	}
	if force && count != 0 {
		fmt.Printf("Atlas WG Mesh is force-removed; detached VM hooks and cleared %d local VM entries\n", count)
		return nil
	}
	fmt.Println("Atlas WG Mesh is removed")
	return nil
}

func addVirtualMachine(interfaceName, addressText string, mtu uint32) error {
	address, err := parseMeshAddress(addressText)
	if err != nil {
		return err
	}
	if mtu == 0 {
		return errors.New("mtu must be greater than 0")
	}
	unlock, err := lockVMState()
	if err != nil {
		return err
	}
	defer unlock()
	// Resolve the interface before changing host state.
	device, err := net.InterfaceByName(interfaceName)
	if err != nil {
		return err
	}
	config, err := readPinnedConfig()
	if err != nil {
		return err
	}
	uplinkName := interfaceWithIPv4(config.UplinkIPv4)
	if uplinkName == "" {
		return errors.New("cannot find the configured uplink")
	}
	if err := runCommand("ip", "-6", "addr", "replace", "fe80::1/64", "dev", interfaceName, "nodad"); err != nil {
		return err
	}
	if err := runCommand("ip", "link", "set", interfaceName, "mtu", fmt.Sprint(mtu), "up"); err != nil {
		return err
	}
	if err := runCommand("ip", "-6", "route", "replace", addressText+"/128", "dev", interfaceName); err != nil {
		return err
	}
	// Proxy NDP first, so the host answers for the VM as soon as it is claimed.
	if err := setProxyNeighbour(addressText, uplinkName); err != nil {
		return err
	}
	if err := addLocalVirtualMachine(address, uint32(device.Index)); err != nil {
		return rollbackVirtualMachineAddition(address, addressText, interfaceName, uplinkName, err)
	}
	if err := attachHook(interfaceName, vmBPFProgram, "ingress"); err != nil {
		return rollbackVirtualMachineAddition(address, addressText, interfaceName, uplinkName, err)
	}
	// Release the state lock before the network notification.
	unlock()

	// Send the multicast announcement on the shared VLAN.
	if err := announceVirtualMachine(address, config); err != nil {
		fmt.Fprintf(os.Stderr, "atlas-wg-mesh: warning: announce %s: %v\n", addressText, err)
	}
	fmt.Printf("VM %s is ready on %s\n", addressText, interfaceName)
	return nil
}

func removeVirtualMachine(interfaceName, addressText string) error {
	address, err := parseMeshAddress(addressText)
	if err != nil {
		return err
	}
	unlock, err := lockVMState()
	if err != nil {
		return err
	}
	defer unlock()
	config, err := readPinnedConfig()
	if err != nil {
		return err
	}
	uplinkName := interfaceWithIPv4(config.UplinkIPv4)
	if uplinkName == "" {
		return errors.New("cannot find the configured uplink")
	}
	if err := removeProxyNeighbour(addressText, uplinkName); err != nil {
		return err
	}
	local, err := isLocalVirtualMachine(address)
	if err != nil {
		return err
	}
	if !local {
		routeRemoved, err := removeVirtualMachineRoute(addressText, interfaceName)
		if err != nil {
			return err
		}
		if routeRemoved {
			fmt.Printf("Removed stale route for VM %s from %s\n", addressText, interfaceName)
			return nil
		}
		return fmt.Errorf("%s is not a registered local VM", addressText)
	}
	device, interfaceExists, err := findNetworkInterface(interfaceName)
	if err != nil {
		return err
	}
	if !interfaceExists {
		if err := removeLocalVirtualMachine(address); err != nil {
			return err
		}
		fmt.Printf("VM %s is removed; interface %s no longer exists\n", addressText, interfaceName)
		return nil
	}
	routeRemoved, err := removeVirtualMachineRoute(addressText, interfaceName)
	if err != nil {
		return err
	}
	hasOtherVM, err := hasOtherLocalVirtualMachineOnInterface(address, uint32(device.Index))
	if err != nil {
		return rollbackVirtualMachineRemoval(routeRemoved, false, addressText, interfaceName, err)
	}
	hookDetached := false
	if !hasOtherVM {
		if err := detachHook(interfaceName); err != nil {
			return rollbackVirtualMachineRemoval(routeRemoved, false, addressText, interfaceName, err)
		}
		hookDetached = true
	}
	if err := removeLocalVirtualMachine(address); err != nil {
		return rollbackVirtualMachineRemoval(routeRemoved, hookDetached, addressText, interfaceName, err)
	}
	fmt.Printf("VM %s is removed from %s\n", addressText, interfaceName)
	return nil
}

func removeVirtualMachineRoute(addressText, interfaceName string) (bool, error) {
	route, err := commandOutput("ip", "-o", "-6", "route", "show", addressText+"/128")
	if err != nil {
		return false, err
	}
	if strings.TrimSpace(route) == "" {
		return false, nil
	}
	if err := runCommand("ip", "-6", "route", "del", addressText+"/128", "dev", interfaceName); err != nil {
		return false, err
	}
	return true, nil
}

func showStatus() error {
	config, err := readPinnedConfig()
	if err != nil {
		return err
	}
	count, err := localVirtualMachineCount()
	if err != nil {
		return err
	}
	remoteCount, remoteCapacity, err := remoteLocationCount()
	if err != nil {
		return err
	}
	privilegedVMs, err := privilegedTenantAllowedAddresses()
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	privilegedCount := "unavailable"
	if err == nil {
		privilegedCount = fmt.Sprint(len(privilegedVMs))
	}
	fmt.Printf("discovery interface: %s\nlocal VMs: %d\nprivileged VMs: %s\nremote locations: %d/%d\nWireGuard address: %s\n", discoveryInterfaceName(config), count, privilegedCount, remoteCount, remoteCapacity, netip.AddrFrom16(config.WireGuardIPv6))
	return nil
}

// discoveryInterfaceName names the interface that carries neighbour
// discovery. A configure run records the uplink, so any other index means the
// state is stale and configure must run again.
func discoveryInterfaceName(config hostConfig) string {
	device, err := net.InterfaceByIndex(int(config.DiscoveryIndex))
	if err != nil {
		return fmt.Sprintf("ifindex:%d (missing; rerun configure)", config.DiscoveryIndex)
	}
	if device.Name == interfaceWithIPv4(config.UplinkIPv4) {
		return device.Name + " (multicast)"
	}
	return device.Name
}
