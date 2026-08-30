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

func installHost(uplinkName, wireGuardName string, whoHasRate, whoHasBurst uint32) error {
	if whoHasRate > 0 && whoHasBurst < 1 {
		return errors.New("who-has-burst must be at least 1 when who-has-rate is set")
	}
	if _, err := os.Stat(filepath.Join(pinDirectory, "config")); err == nil {
		return errors.New("Atlas WG Mesh is already configured")
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}

	config, err := readHostConfig(uplinkName, wireGuardName)
	if err != nil {
		return err
	}
	config.WhoHasRate = whoHasRate
	config.WhoHasBurst = whoHasBurst

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
	if err := pinCollection(collection, config); err != nil {
		return rollbackInstall(err)
	}
	if err := attachHook(uplinkName, uplinkProgram); err != nil {
		return rollbackInstall(err)
	}
	if err := attachHook(wireGuardName, wireguardProgram); err != nil {
		return rollbackInstall(err, uplinkName)
	}
	fmt.Printf("Atlas WG Mesh is installed on %s and %s\n", uplinkName, wireGuardName)
	return nil
}

func rollbackInstall(cause error, interfaceNames ...string) error {
	var detachErr error
	for _, interfaceName := range interfaceNames {
		if err := detachHook(interfaceName); err != nil {
			detachErr = errors.Join(detachErr, fmt.Errorf("detach %s hook: %w", interfaceName, err))
		}
	}
	if detachErr != nil {
		return errors.Join(cause, detachErr)
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
			detached[name] = struct{}{}
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
	if err := runCommand("ip", "-6", "addr", "replace", "fe80::1/64", "dev", interfaceName, "nodad"); err != nil {
		return err
	}
	if err := runCommand("ip", "link", "set", interfaceName, "mtu", fmt.Sprint(mtu), "up"); err != nil {
		return err
	}
	if err := runCommand("ip", "-6", "route", "replace", addressText+"/128", "dev", interfaceName); err != nil {
		return err
	}
	if err := addLocalVirtualMachine(address, uint32(device.Index)); err != nil {
		if routeErr := runCommand("ip", "-6", "route", "del", addressText+"/128", "dev", interfaceName); routeErr != nil {
			return errors.Join(err, fmt.Errorf("remove route for %s: %w", addressText, routeErr))
		}
		return err
	}
	if err := attachHook(interfaceName, vmBPFProgram); err != nil {
		return rollbackVirtualMachineAddition(address, uint32(device.Index), addressText, interfaceName, err)
	}
	// Release the state lock before the network notification.
	unlock()

	// A missed announcement is repaired by WHO_HAS.
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
	fmt.Printf("uplink index: %d\nlocal VMs: %d\nremote locations: %d/%d\nWireGuard address: %s\n", config.UplinkIndex, count, remoteCount, remoteCapacity, netip.AddrFrom16(config.WireGuardIPv6))
	if config.WhoHasRate == 0 {
		fmt.Println("WHO_HAS rate limit: disabled")
	} else {
		fmt.Printf("WHO_HAS rate limit: %d/s burst %d\n", config.WhoHasRate, config.WhoHasBurst)
	}
	return nil
}
