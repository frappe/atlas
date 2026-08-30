package main

import (
	"bytes"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"fmt"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"sort"

	"github.com/cilium/ebpf"
)

const (
	vmBPFProgram     = "handle_vm_packet"
	uplinkProgram    = "handle_uplink_packet"
	wireguardProgram = "handle_wireguard_packet"
)

//go:embed atlas-wg-mesh.bpf.o
var bpfObject []byte

// Must match struct config in bpf/state.h.
type hostConfig struct {
	UplinkIndex   uint32
	UplinkIPv4    [4]byte
	UplinkMAC     [6]byte
	Padding       [2]byte
	WireGuardIPv6 [16]byte
	WhoHasRate    uint32
	WhoHasBurst   uint32
}

// programPath returns the pin for a program. A fresh install pins at the top
// level of the pin directory. An upgrade pins under releases/<hash> and
// removes the top level pin, so look for the installed release first.
func programPath(program string) (string, error) {
	if hash, err := readInstalledHash(); err == nil {
		release := filepath.Join(pinDirectory, "releases", hex.EncodeToString(hash[:]), program)
		if _, err := os.Stat(release); err == nil {
			return release, nil
		}
	}
	top := filepath.Join(pinDirectory, program)
	if _, err := os.Stat(top); err != nil {
		return "", fmt.Errorf("no pinned program %s: run configure or upgrade", program)
	}
	return top, nil
}

func pinCollection(collection *ebpf.Collection, config hostConfig) error {
	if err := os.MkdirAll(pinDirectory, 0755); err != nil {
		return err
	}
	for name, bpfMap := range collection.Maps {
		if err := bpfMap.Pin(filepath.Join(pinDirectory, name)); err != nil {
			return err
		}
	}
	for name, program := range collection.Programs {
		if err := program.Pin(filepath.Join(pinDirectory, name)); err != nil {
			return err
		}
	}
	if err := collection.Maps["config"].Put(uint32(0), config); err != nil {
		return err
	}
	return collection.Maps["build_hash"].Put(uint32(0), bpfHash())
}

func loadCollection(replacements map[string]*ebpf.Map) (*ebpf.Collection, error) {
	spec, err := ebpf.LoadCollectionSpecFromReader(bytes.NewReader(bpfObject))
	if err != nil {
		return nil, fmt.Errorf("read embedded BPF object: %w", err)
	}
	collection, err := ebpf.NewCollectionWithOptions(spec, ebpf.CollectionOptions{MapReplacements: replacements})
	if err != nil {
		return nil, fmt.Errorf("load BPF programs: %w", err)
	}
	return collection, nil
}

func bpfHash() [32]byte {
	return sha256.Sum256(bpfObject)
}

func readInstalledHash() ([32]byte, error) {
	buildMap, err := openMap("build_hash")
	if err != nil {
		return [32]byte{}, err
	}
	defer buildMap.Close()

	var hash [32]byte
	if err := buildMap.Lookup(uint32(0), &hash); err != nil {
		return [32]byte{}, err
	}
	return hash, nil
}

func openMap(name string) (*ebpf.Map, error) {
	return ebpf.LoadPinnedMap(filepath.Join(pinDirectory, name), nil)
}

func clearPinDirectory() error {
	entries, err := os.ReadDir(pinDirectory)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if err := os.RemoveAll(filepath.Join(pinDirectory, entry.Name())); err != nil {
			return err
		}
	}
	return nil
}

func readPinnedConfig() (hostConfig, error) {
	configMap, err := openMap("config")
	if err != nil {
		return hostConfig{}, err
	}
	defer configMap.Close()

	var config hostConfig
	if err := configMap.Lookup(uint32(0), &config); err != nil {
		return hostConfig{}, err
	}
	return config, nil
}

func addLocalVirtualMachine(address [16]byte, ifindex uint32) error {
	vmMap, err := openMap("local_vms")
	if err != nil {
		return err
	}
	defer vmMap.Close()

	return vmMap.Put(address, ifindex)
}

func removeLocalVirtualMachine(address [16]byte) error {
	vmMap, err := openMap("local_vms")
	if err != nil {
		return err
	}
	defer vmMap.Close()

	return vmMap.Delete(address)
}

func hasOtherLocalVirtualMachineOnInterface(address [16]byte, ifindex uint32) (bool, error) {
	vmMap, err := openMap("local_vms")
	if err != nil {
		return false, err
	}
	defer vmMap.Close()

	var otherAddress [16]byte
	var otherIndex uint32
	iterator := vmMap.Iterate()
	for iterator.Next(&otherAddress, &otherIndex) {
		if otherAddress != address && otherIndex == ifindex {
			return true, nil
		}
	}
	return false, iterator.Err()
}

type localVirtualMachine struct {
	address       netip.Addr
	ifindex       uint32
	interfaceName string
}

func (virtualMachine localVirtualMachine) interfaceLabel() string {
	if virtualMachine.interfaceName != "" {
		return virtualMachine.interfaceName
	}
	return fmt.Sprintf("ifindex:%d", virtualMachine.ifindex)
}

func localVirtualMachines() ([]localVirtualMachine, error) {
	vmMap, err := openMap("local_vms")
	if err != nil {
		return nil, err
	}
	defer vmMap.Close()

	virtualMachines := make([]localVirtualMachine, 0)
	var address [16]byte
	var ifindex uint32
	iterator := vmMap.Iterate()
	for iterator.Next(&address, &ifindex) {
		interfaceName := ""
		if device, err := net.InterfaceByIndex(int(ifindex)); err == nil {
			interfaceName = device.Name
		}
		virtualMachines = append(virtualMachines, localVirtualMachine{
			address:       netip.AddrFrom16(address),
			ifindex:       ifindex,
			interfaceName: interfaceName,
		})
	}
	if err := iterator.Err(); err != nil {
		return nil, err
	}
	sort.Slice(virtualMachines, func(left, right int) bool {
		return virtualMachines[left].address.Less(virtualMachines[right].address)
	})
	return virtualMachines, nil
}

func localVirtualMachineCount() (int, error) {
	vmMap, err := openMap("local_vms")
	if err != nil {
		return 0, err
	}
	defer vmMap.Close()

	var address [16]byte
	var value uint32
	count := 0
	iterator := vmMap.Iterate()
	for iterator.Next(&address, &value) {
		count++
	}
	return count, iterator.Err()
}
