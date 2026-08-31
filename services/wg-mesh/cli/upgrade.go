package main

import (
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strings"

	"github.com/cilium/ebpf"
	"github.com/spf13/cobra"
)

var version = "dev"

var upgradeForce bool

var upgradeCommand = &cobra.Command{
	Use:   "upgrade",
	Short: "replace BPF programs with this binary's version",
	Args:  cobra.NoArgs,
	RunE: func(*cobra.Command, []string) error {
		return upgradeBPF(upgradeForce)
	},
}

func showVersion() error {
	installed, err := readInstalledHash()
	fmt.Printf("CLI version: %s\nEmbedded BPF SHA-256: %x\n", version, bpfHash())
	if err == nil {
		fmt.Printf("Installed BPF SHA-256: %x\n", installed)
	} else {
		fmt.Printf("Installed BPF SHA-256: unavailable (%v)\n", err)
	}
	return nil
}

func upgradeBPF(force bool) error {
	unlock, err := lockVMState()
	if err != nil {
		return err
	}
	defer unlock()

	installed, err := readInstalledHash()
	if err == nil && installed == bpfHash() {
		fmt.Println("Atlas WG Mesh BPF is already current")
		return nil
	}
	config, err := readPinnedConfig()
	if err != nil {
		return err
	}
	replacements, closeMaps, err := existingMaps()
	if err != nil {
		return err
	}
	defer closeMaps()

	collection, err := loadCollection(replacements)
	if err != nil {
		if force {
			return forceUpgrade(config)
		}
		return fmt.Errorf("BPF maps are incompatible; rerun with --force after adding a migration: %w", err)
	}
	defer collection.Close()
	for name, bpfMap := range collection.Maps {
		if _, exists := replacements[name]; exists {
			continue
		}
		if err := bpfMap.Pin(filepath.Join(pinDirectory, name)); err != nil {
			return err
		}
	}

	candidateHash := bpfHash()
	hash := hex.EncodeToString(candidateHash[:])
	release := filepath.Join(pinDirectory, "releases", hash)
	if err := os.MkdirAll(release, 0755); err != nil {
		return err
	}
	for name, program := range collection.Programs {
		if err := program.Pin(filepath.Join(release, name)); err != nil && !errors.Is(err, os.ErrExist) {
			return err
		}
	}
	interfaces, err := configuredInterfaces()
	if err != nil {
		return err
	}
	if interfaces.uplinkName == "" || interfaces.wireGuardName == "" {
		return fmt.Errorf("cannot find configured uplink and WireGuard interfaces")
	}
	if err := attachHookPath(interfaces.uplinkName, filepath.Join(release, uplinkProgram)); err != nil {
		return err
	}
	if err := attachHookPath(interfaces.wireGuardName, filepath.Join(release, wireguardProgram)); err != nil {
		return err
	}
	vmInterfaces, err := virtualMachineInterfaces()
	if err != nil {
		return err
	}
	for _, interfaceName := range vmInterfaces {
		if err := attachHookPath(interfaceName, filepath.Join(release, vmBPFProgram)); err != nil {
			return err
		}
	}
	if err := collection.Maps["config"].Put(uint32(0), config); err != nil {
		return err
	}
	if err := collection.Maps["build_hash"].Put(uint32(0), bpfHash()); err != nil {
		return err
	}
	for _, program := range []string{vmBPFProgram, uplinkProgram, wireguardProgram} {
		_ = os.Remove(filepath.Join(pinDirectory, program))
	}
	cleanReleases(hash, installed)
	fmt.Printf("Atlas WG Mesh BPF upgraded to %s\n", hash[:12])
	return nil
}

func forceUpgrade(config hostConfig) error {
	vmInterfaces, err := virtualMachineInterfaces()
	if err != nil {
		return err
	}
	interfaces, err := configuredInterfaces()
	if err != nil {
		return err
	}
	if interfaces.uplinkName == "" || interfaces.wireGuardName == "" {
		return fmt.Errorf("cannot find configured uplink and WireGuard interfaces")
	}

	// Load first. A verifier rejection must leave the host untouched,
	// rather than strip its BPF and leave the TC filters dangling.
	collection, err := loadCollection(nil)
	if err != nil {
		return err
	}
	defer collection.Close()

	if err := clearPinDirectory(); err != nil {
		return err
	}
	if err := pinCollection(collection, config); err != nil {
		return err
	}
	if err := attachHook(interfaces.uplinkName, uplinkProgram); err != nil {
		return err
	}
	if err := attachHook(interfaces.wireGuardName, wireguardProgram); err != nil {
		return err
	}
	for vm, interfaceName := range vmInterfaces {
		if err := attachHook(interfaceName, vmBPFProgram); err != nil {
			return err
		}
		device, err := net.InterfaceByName(interfaceName)
		if err != nil {
			return err
		}
		if err := addLocalVirtualMachine(vm, uint32(device.Index)); err != nil {
			return err
		}
	}
	hash := bpfHash()
	fmt.Printf("Atlas WG Mesh BPF force-upgraded to %x; learned remote locations were cleared\n", hash[:6])
	return nil
}

// virtualMachineInterfaces maps every registered VM to its host interface. It
// reads the keys with a raw value buffer sized to the map itself, so it still
// works when the value layout of local_vms has changed, which is exactly when
// a forced migration is needed.
func virtualMachineInterfaces() (map[[16]byte]string, error) {
	vmMap, err := openMap("local_vms")
	if err != nil {
		return nil, err
	}
	defer vmMap.Close()

	value := make([]byte, vmMap.ValueSize())
	var vm [16]byte
	interfaces := make(map[[16]byte]string)
	iterator := vmMap.Iterate()
	for iterator.Next(&vm, &value) {
		address := netip.AddrFrom16(vm).String()
		output, err := commandOutput("ip", "-o", "-6", "route", "show", address+"/128")
		if err != nil {
			return nil, err
		}
		interfaceName := fieldAfter(strings.Fields(output), "dev")
		if interfaceName == "" {
			return nil, fmt.Errorf("no route for local VM %s", address)
		}
		interfaces[vm] = interfaceName
	}
	return interfaces, iterator.Err()
}

func existingMaps() (map[string]*ebpf.Map, func(), error) {
	names := []string{"config", "local_vms", privilegedTenantAllowedAddressesMap, "remote_vms", "discovery_limits", "debug_config", "debug_stats", "debug_events", "build_hash"}
	maps := make(map[string]*ebpf.Map)
	for _, name := range names {
		bpfMap, err := openMap(name)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return nil, func() {}, err
		}
		maps[name] = bpfMap
	}
	return maps, func() {
		for _, bpfMap := range maps {
			bpfMap.Close()
		}
	}, nil
}

func cleanReleases(current string, previous [32]byte) {
	entries, err := os.ReadDir(filepath.Join(pinDirectory, "releases"))
	if err != nil {
		return
	}
	previousName := hex.EncodeToString(previous[:])
	for _, entry := range entries {
		if entry.Name() == current || entry.Name() == previousName {
			continue
		}
		_ = os.RemoveAll(filepath.Join(pinDirectory, "releases", entry.Name()))
	}
}
