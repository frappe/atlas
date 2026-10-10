package main

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"
)

const vmLockPath = "/run/lock/atlas-wg-mesh.lock"

var uplinkName, wireGuardName, controllerText string

var resetForce bool
var statusJSON bool

var configureCommand = &cobra.Command{
	Use:   "configure",
	Short: "install or refresh Atlas WG Mesh on this host",
	Args:  cobra.NoArgs,
	RunE: func(command *cobra.Command, _ []string) error {
		var controller *[16]byte
		if command.Flags().Changed("controller") {
			address, err := parseController(controllerText)
			if err != nil {
				return err
			}
			controller = &address
		}
		return configureHost(uplinkName, wireGuardName, controller)
	},
}

var resetCommand = &cobra.Command{
	Use:   "reset",
	Short: "remove Atlas WG Mesh from this host",
	Args:  cobra.NoArgs,
	RunE: func(*cobra.Command, []string) error {
		return resetHost(resetForce)
	},
}

var statusCommand = &cobra.Command{
	Use:   "status",
	Short: "show local Atlas WG Mesh state",
	Args:  cobra.NoArgs,
	RunE: func(*cobra.Command, []string) error {
		return showStatus()
	},
}

var versionCommand = &cobra.Command{
	Use:   "version",
	Short: "show CLI and BPF versions",
	Args:  cobra.NoArgs,
	RunE: func(*cobra.Command, []string) error {
		return showVersion()
	},
}

func init() {
	configureCommand.Flags().StringVar(&uplinkName, "uplink", "", "mesh uplink interface")
	configureCommand.Flags().StringVar(&wireGuardName, "wireguard", "", "WireGuard interface")
	configureCommand.Flags().StringVar(&controllerText, "controller", "", "Atlas tenant-0 mesh address on wg0")
	configureCommand.MarkFlagRequired("uplink")
	configureCommand.MarkFlagRequired("wireguard")
	resetCommand.Flags().BoolVar(&resetForce, "force", false, "remove mesh state while local VMs remain")
	statusCommand.Flags().BoolVar(&statusJSON, "json", false, "print JSON")

	rootCommand.AddCommand(configureCommand, resetCommand, statusCommand, versionCommand)
}

// parseController accepts an empty value or a tenant-0 VM address.
func parseController(text string) ([16]byte, error) {
	if text == "" {
		return [16]byte{}, nil
	}
	address, err := parseMeshAddress(text)
	if err != nil {
		return [16]byte{}, err
	}
	if address[4]|address[5]|address[6]|address[7] != 0 {
		return [16]byte{}, fmt.Errorf("controller %s is not a tenant-0 address", text)
	}
	return address, nil
}

// configureHost installs the mesh or replaces its BPF without detaching a live hook. A nil controller keeps the stored address.
func configureHost(uplinkName, wireGuardName string, controller *[16]byte) error {
	unlock, err := lockFile(vmLockPath, true)
	if err != nil {
		return err
	}
	defer unlock()

	config, err := readHostConfig(uplinkName, wireGuardName)
	if err != nil {
		return err
	}

	if pinned, err := readPinnedConfig(); err == nil {
		if pinned.UplinkIfIndex != config.UplinkIfIndex {
			return fmt.Errorf("the mesh runs on %s: reset the host to change the uplink", deviceName(pinned.UplinkIfIndex))
		}
		if pinned.PublicIfIndex != config.PublicIfIndex || pinned.WireGuardIPv6 != config.WireGuardIPv6 {
			return fmt.Errorf("reset the host to change its public or WireGuard interface")
		}
	}

	// Read the VMs before a new release can recreate local_vms empty.
	vmInterfaces, err := readVMInterfaces()
	if err != nil {
		return err
	}

	if err := prepareHost(uplinkName); err != nil {
		return err
	}

	oldHash, oldHashError := readMap[[32]byte]("build_hash", uint32(0))
	candidate, err := loadBPF(config)
	if err != nil {
		return err
	}

	if err := restoreLocalVMs(candidate.createdMaps, vmInterfaces); err != nil {
		candidate.cleanup()
		return err
	}

	hooks := hostHooks(config, wireGuardName, vmInterfaces)
	if err := replaceHooks(hooks, candidate.hash, oldHash, oldHashError == nil); err != nil {
		candidate.cleanup()
		return err
	}

	if err := candidate.commit(); err != nil {
		rollbackError := rollbackHooks(hooks, oldHash, oldHashError == nil)
		candidate.cleanup()
		return errors.Join(err, rollbackError)
	}
	if controller != nil {
		if err := writeMap("controller_address", uint32(0), *controller); err != nil {
			return fmt.Errorf("write the controller address: %w", err)
		}
	}

	fmt.Printf("Atlas WG Mesh runs on %s and %s\n", uplinkName, wireGuardName)
	return errors.Join(removeOldReleases(), removeObsoleteMaps(candidate.maps))
}

// prepareHost sets the kernel state the mesh needs. peers sync sets the NDP mode.
func prepareHost(uplinkName string) error {
	if err := runCommand("mountpoint", "-q", "/sys/fs/bpf"); err != nil {
		if err := runCommand("mount", "-t", "bpf", "bpf", "/sys/fs/bpf"); err != nil {
			return err
		}
	}

	if err := os.MkdirAll(pinDirectory, 0755); err != nil {
		return err
	}

	// Write procfs directly because sysctl treats a dot in a VLAN name as a separator.
	// A zero proxy delay prevents the mesh from dropping packets while Linux waits to answer.
	for path, value := range map[string]string{
		"/proc/sys/net/ipv6/conf/" + uplinkName + "/proxy_ndp":    "1\n",
		"/proc/sys/net/ipv6/neigh/" + uplinkName + "/proxy_delay": "0\n",
	} {
		if err := os.WriteFile(path, []byte(value), 0644); err != nil {
			return err
		}
	}

	for _, arguments := range [][]string{
		{"sysctl", "-qw", "net.ipv6.conf.all.forwarding=1"},
		{"ip", "-6", "route", "replace", meshRoutePrefix, "dev", uplinkName},
	} {
		if err := runCommand(arguments[0], arguments[1:]...); err != nil {
			return err
		}
	}

	return nil
}

type programHook struct {
	interfaceName string
	program       string
	direction     string
	wasAttached   bool
}

// hostHooks returns each live mesh hook once. The egress hook exists only in unicast mode.
func hostHooks(config hostConfig, wireGuardName string, vmInterfaces map[[16]byte]string) []programHook {
	uplinkName := deviceName(config.UplinkIfIndex)
	wanted := []programHook{
		{interfaceName: uplinkName, program: uplinkIngressProgram, direction: "ingress"},
		{interfaceName: deviceName(config.PublicIfIndex), program: uplinkIngressProgram, direction: "ingress"},
		{interfaceName: wireGuardName, program: wireGuardProgram, direction: "ingress"},
	}

	if isHookAttached(uplinkName, "egress") {
		wanted = append(wanted, programHook{interfaceName: uplinkName, program: uplinkEgressProgram, direction: "egress"})
	}

	for _, name := range vmInterfaces {
		wanted = append(wanted, programHook{interfaceName: name, program: vmProgram, direction: "ingress"})
	}

	seen := make(map[string]bool)
	hooks := make([]programHook, 0, len(wanted))
	for _, hook := range wanted {
		key := hook.interfaceName + "\x00" + hook.direction
		if seen[key] {
			continue
		}
		seen[key] = true
		hook.wasAttached = isHookAttached(hook.interfaceName, hook.direction)
		hooks = append(hooks, hook)
	}

	return hooks
}

func replaceHooks(hooks []programHook, newHash, oldHash [32]byte, hasOldRelease bool) error {
	for index, hook := range hooks {
		if hook.wasAttached && !hasOldRelease {
			return fmt.Errorf("%s %s has a mesh hook but no installed release", hook.interfaceName, hook.direction)
		}

		if err := attachProgram(hook.interfaceName, programPathFor(newHash, hook.program), hook.direction); err != nil {
			return errors.Join(err, rollbackHooks(hooks[:index], oldHash, hasOldRelease))
		}
	}

	return nil
}

func rollbackHooks(hooks []programHook, oldHash [32]byte, hasOldRelease bool) error {
	var rollbackErrors []error
	for index := len(hooks) - 1; index >= 0; index-- {
		hook := hooks[index]
		if hook.wasAttached && hasOldRelease {
			rollbackErrors = append(rollbackErrors, attachProgram(hook.interfaceName, programPathFor(oldHash, hook.program), hook.direction))
		} else {
			rollbackErrors = append(rollbackErrors, detachHook(hook.interfaceName, hook.direction))
		}
	}

	return errors.Join(rollbackErrors...)
}

// restoreLocalVMs refills local_vms when a first installation creates the map.
func restoreLocalVMs(created []string, vmInterfaces map[[16]byte]string) error {
	createdLocalVMs := false
	for _, name := range created {
		createdLocalVMs = createdLocalVMs || name == "local_vms"
	}
	if !createdLocalVMs {
		return nil
	}

	for address, name := range vmInterfaces {
		device, err := net.InterfaceByName(name)
		if err != nil {
			return err
		}
		if err := writeMap("local_vms", address, uint32(device.Index)); err != nil {
			return err
		}
	}

	return nil
}

// readVMInterfaces maps every local VM to the interface of its host route. It reads keys only, so it works with any value layout.
func readVMInterfaces() (map[[16]byte]string, error) {
	vmInterfaces := make(map[[16]byte]string)

	vms, err := openMap("local_vms")
	if errors.Is(err, os.ErrNotExist) {
		return vmInterfaces, nil
	}
	if err != nil {
		return nil, err
	}
	defer vms.Close()

	var address [16]byte
	value := make([]byte, vms.ValueSize())

	iterator := vms.Iterate()
	for iterator.Next(&address, &value) {
		text := netip.AddrFrom16(address).String()
		output, err := commandOutput("ip", "-6", "route", "show", text+"/128")
		if err != nil {
			return nil, err
		}

		name := fieldAfter(strings.Fields(output), "dev")
		if name == "" {
			return nil, fmt.Errorf("local VM %s has no host route", text)
		}
		vmInterfaces[address] = name
	}

	return vmInterfaces, iterator.Err()
}

// resetHost detaches every hook and removes the pinned state. Without force it refuses while VMs remain.
func resetHost(force bool) error {
	unlock, err := lockFile(vmLockPath, true)
	if err != nil {
		return err
	}
	defer unlock()

	config, err := readPinnedConfig()
	if err != nil {
		return err
	}

	vms, err := readLocalVMs()
	if err != nil {
		return err
	}
	if len(vms) > 0 && !force {
		return fmt.Errorf("%d local VMs remain: remove them or use --force", len(vms))
	}

	uplinkName := deviceName(config.UplinkIfIndex)
	interfaces := map[string]bool{uplinkName: true, deviceName(config.PublicIfIndex): true, interfaceWithAddress(config.WireGuardIPv6): true}
	for address, ifindex := range vms {
		interfaces[deviceName(ifindex)] = true
		_ = ignoreMissing(runCommand("ip", "-6", "neigh", "del", "proxy", netip.AddrFrom16(address).String(), "dev", uplinkName))
	}

	for name := range interfaces {
		if name == "" {
			continue
		}
		for _, direction := range []string{"ingress", "egress"} {
			if err := detachHook(name, direction); err != nil {
				return err
			}
		}
	}

	if err := ignoreMissing(runCommand("ip", "-6", "route", "del", meshRoutePrefix, "dev", uplinkName)); err != nil {
		return err
	}

	if err := os.RemoveAll(pinDirectory); err != nil {
		return err
	}

	fmt.Println("Atlas WG Mesh is removed")
	return nil
}

func showStatus() error {
	config, err := readPinnedConfig()
	if err != nil {
		return err
	}

	vms, err := readLocalVMs()
	if err != nil {
		return err
	}

	privileged, err := readMapEntries[[16]byte, uint8]("privileged_vms")
	if err != nil {
		return err
	}

	peers, err := readPeers()
	if err != nil {
		return err
	}

	hash, err := readMap[[32]byte]("build_hash", uint32(0))
	if err != nil {
		return err
	}
	uplink := deviceName(config.UplinkIfIndex)
	mode := "multicast"
	if isHookAttached(uplink, "egress") {
		mode = "unicast"
	}
	status := struct {
		Uplink        string `json:"uplink"`
		Public        string `json:"public"`
		WireGuard     string `json:"wireguard"`
		WireGuardIPv6 string `json:"wireguard_ipv6"`
		NDPMode       string `json:"ndp_mode"`
		BPFHash       string `json:"bpf_hash"`
		LocalVMs      int    `json:"local_vms"`
		PrivilegedVMs int    `json:"privileged_vms"`
		Peers         int    `json:"peers"`
	}{
		Uplink: uplink, Public: deviceName(config.PublicIfIndex),
		WireGuard: interfaceWithAddress(config.WireGuardIPv6), WireGuardIPv6: netip.AddrFrom16(config.WireGuardIPv6).String(),
		NDPMode: mode, BPFHash: hex.EncodeToString(hash[:]),
		LocalVMs: len(vms), PrivilegedVMs: len(privileged), Peers: len(peers),
	}
	if statusJSON {
		return json.NewEncoder(os.Stdout).Encode(status)
	}
	fmt.Printf("uplink\t%s\npublic interface\t%s\nWireGuard interface\t%s\nWireGuard address\t%s\nNDP mode\t%s\nBPF\t%s\nlocal VMs\t%d\nprivileged VMs\t%d\npeers\t%d\n",
		status.Uplink, status.Public, status.WireGuard, status.WireGuardIPv6, status.NDPMode, status.BPFHash,
		status.LocalVMs, status.PrivilegedVMs, status.Peers)
	return nil
}

func showVersion() error {
	fmt.Printf("CLI version: %s\nEmbedded BPF: %x\n", version, bpfHash())

	path, err := programPath(vmProgram)
	if err != nil {
		fmt.Printf("Installed BPF: none (%v)\n", err)
		return nil
	}

	fmt.Printf("Installed BPF: %s\n", filepath.Base(filepath.Dir(path)))
	return nil
}
