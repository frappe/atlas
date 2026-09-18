package main

import (
	"bytes"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"sort"

	"github.com/cilium/ebpf"
	"github.com/cilium/ebpf/link"
)

const (
	vmBPFProgram     = "handle_vm_packet"
	ndpProgram       = "handle_ndp_packet"
	wireguardProgram = "handle_wireguard_packet"

	nudFailureProgram   = "handle_atlas_nud_failure"
	nudReachableProgram = "handle_atlas_nud_reachable"

	ndpUnicastEgressProgram  = "handle_ndp_unicast_egress"
	ndpUnicastIngressProgram = "handle_ndp_unicast_ingress"
)

// The NUD hooks are not TC hooks: they attach to neighbour tracepoints.
const nudTracepointCategory = "neigh"

const (
	nudFailureTracepointName   = "neigh_timer_handler"
	nudReachableTracepointName = "neigh_update"
)

//go:embed atlas-wg-mesh.bpf.o
var bpfObject []byte

// Must match struct config in bpf/state.h. DiscoveryIndex is the shared
// VLAN/uplink interface for multicast NDP discovery, and DiscoveryMAC is
// its MAC. The trailing padding keeps the Go layout aligned with the BPF
// struct.
type hostConfig struct {
	DiscoveryIndex uint32
	UplinkIPv4     [4]byte
	WireGuardIPv6  [16]byte
	DiscoveryMAC   [6]byte
	_              [2]byte
}

// programPath returns the pin for a program: the installed release first,
// then the top level pin of a fresh install.
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

// nudHookLinkPinPath returns the stable pin path for a NUD tracepoint link.
// Each NUD program has its own link.
func nudHookLinkPinPath(program string) string {
	return filepath.Join(pinDirectory, "links", program)
}

// attachNUDHook attaches the pinned program that programPath finds for the
// given program name. A pinned link keeps the hook attached after this
// process exits; an existing link is replaced.
func attachNUDHook(programName string, tracepointName string) error {
	bpfProgramPath, err := programPath(programName)
	if err != nil {
		return err
	}

	return attachNUDHookPath(programName, tracepointName, bpfProgramPath)
}

// attachNUDHookPath attaches the NUD tracepoint program at the given path
// and pins its link, replacing any existing link.
func attachNUDHookPath(programName string, tracepointName string, bpfProgramPath string) error {
	program, err := ebpf.LoadPinnedProgram(bpfProgramPath, nil)
	if err != nil {
		return fmt.Errorf("load the pinned NUD program %s: %w", bpfProgramPath, err)
	}
	defer program.Close()

	linkPinPath := nudHookLinkPinPath(programName)

	existingLink, err := link.LoadPinnedLink(linkPinPath, nil)
	if err == nil {
		unpinError := existingLink.Unpin()

		var closeError error
		if unpinError == nil {
			// The pin held the last reference, so closing releases the
			// link and detaches its program.
			closeError = existingLink.Close()
		}

		if unpinError != nil || closeError != nil {
			return fmt.Errorf("replace the pinned NUD link %s: %w", programName, errors.Join(unpinError, closeError))
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("open the pinned NUD link %s: %w", programName, err)
	}

	newLink, err := link.Tracepoint(nudTracepointCategory, tracepointName, program, nil)
	if err != nil {
		return fmt.Errorf("attach %s to the %s/%s tracepoint: %w", programName, nudTracepointCategory, tracepointName, err)
	}

	if err := os.MkdirAll(filepath.Dir(linkPinPath), 0755); err != nil {
		newLink.Close()
		return fmt.Errorf("create NUD link pin directory: %w", err)
	}

	if err := newLink.Pin(linkPinPath); err != nil {
		newLink.Close()
		return fmt.Errorf("pin the NUD tracepoint link %s: %w", programName, err)
	}

	// The pin holds the attachment, so releasing this handle is safe.
	return newLink.Close()
}

// attachNUDHooks attaches both NUD tracepoint programs:
// neigh_timer_handler for failure tracking, neigh_update for reachability.
func attachNUDHooks() error {
	if err := attachNUDHook(nudFailureProgram, nudFailureTracepointName); err != nil {
		return err
	}

	if err := attachNUDHook(nudReachableProgram, nudReachableTracepointName); err != nil {
		return err
	}

	return nil
}

// attachNUDHooksFromRelease attaches both NUD tracepoint programs from a
// release pin directory. The pins of the previous install may not contain
// these programs, and may hold an older program version.
func attachNUDHooksFromRelease(release string) error {
	if err := attachNUDHookPath(nudFailureProgram, nudFailureTracepointName, filepath.Join(release, nudFailureProgram)); err != nil {
		return err
	}

	if err := attachNUDHookPath(nudReachableProgram, nudReachableTracepointName, filepath.Join(release, nudReachableProgram)); err != nil {
		return err
	}

	return nil
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

	collection, err := ebpf.NewCollectionWithOptions(
		spec,
		ebpf.CollectionOptions{
			MapReplacements: replacements,
		},
	)
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
		if otherAddress != address &&
			otherIndex == ifindex {
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

		virtualMachines = append(
			virtualMachines,
			localVirtualMachine{
				address:       netip.AddrFrom16(address),
				ifindex:       ifindex,
				interfaceName: interfaceName,
			},
		)
	}

	if err := iterator.Err(); err != nil {
		return nil, err
	}

	sort.Slice(
		virtualMachines,
		func(left, right int) bool {
			return virtualMachines[left].address.Less(virtualMachines[right].address)
		},
	)

	return virtualMachines, nil
}

// remoteLocationCount returns learned-cache occupancy and capacity.
// A full LRU evicts entries, which NDP learns again.
func remoteLocationCount() (int, uint32, error) {
	remoteMap, err := openMap("remote_vms")
	if err != nil {
		return 0, 0, err
	}
	defer remoteMap.Close()

	var vm, host [16]byte
	count := 0

	iterator := remoteMap.Iterate()

	for iterator.Next(&vm, &host) {
		count++
	}

	return count, remoteMap.MaxEntries(), iterator.Err()
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
