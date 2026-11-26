package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"
)

// Host WireGuard addresses. A peer outside it would receive tunnels for VMs.
var underlayPrefix = netip.MustParsePrefix("fdab::/16")

var peersCommand = &cobra.Command{
	Use:   "peers",
	Short: "manage mesh peers",
	Args:  cobra.NoArgs,
}

var syncPeersCommand = &cobra.Command{
	Use:   "sync PEERS_JSON",
	Short: "load the peers from the WireGuard peer state",
	Args:  cobra.ExactArgs(1),
	RunE: func(_ *cobra.Command, arguments []string) error {
		return syncPeers(arguments[0], peersUnicast, peersTunnels)
	},
}

var peersUnicast bool
var peersTunnels []string

func init() {
	syncPeersCommand.Flags().BoolVar(&peersUnicast, "unicast", false, "carry NDP in IPv4 packets")
	syncPeersCommand.Flags().StringArrayVar(&peersTunnels, "tunnel", nil, "WireGuard interface that mesh tunnels spread over, once for each interface")
	peersCommand.AddCommand(syncPeersCommand)
	rootCommand.AddCommand(peersCommand)
}

// peerState is one entry of the WireGuard peer state file that metald writes.
type peerState struct {
	MeshAddress    string `json:"mesh_address"`
	PublicAddress  string `json:"public_address"`
	PrivateAddress string `json:"private_address"`
	MAC            string `json:"private_network_mac_address"`
}

// syncPeers replaces the peer map, its NDP transport mode, and the mesh tunnels together.
func syncPeers(path string, unicast bool, tunnels []string) error {
	peers, err := readPeerState(path)
	if err != nil {
		return err
	}

	unlock, err := lockFile(vmLockPath, true)
	if err != nil {
		return err
	}
	defer unlock()

	current, err := readPeerArray()
	if err != nil {
		return err
	}
	config, err := readPinnedConfig()
	if err != nil {
		return err
	}
	uplink := deviceName(config.UplinkIfIndex)
	wasUnicast := isHookAttached(uplink, "egress")

	if err := writePeerArray(peers); err != nil {
		_ = writePeerArray(current)
		return err
	}
	if err := setUnicastNDP(uplink, unicast); err != nil {
		return errors.Join(err, writePeerArray(current), setUnicastNDP(uplink, wasUnicast))
	}
	if err := setMeshTunnels(tunnels); err != nil {
		return errors.Join(err, writePeerArray(current), setUnicastNDP(uplink, wasUnicast))
	}

	mode := "multicast"
	if unicast {
		mode = "unicast"
	}
	fmt.Printf("Atlas WG Mesh holds %d peers in %s mode over %d tunnels\n", len(peers), mode, len(tunnels))
	return nil
}

func setMeshTunnels(names []string) error {
	if len(names) > meshTunnelLimit {
		return fmt.Errorf("mesh tunnels hold at most %d interfaces", meshTunnelLimit)
	}

	tunnels := meshTunnels{Count: uint32(len(names))}
	for index, name := range names {
		device, err := net.InterfaceByName(name)
		if err != nil {
			return fmt.Errorf("mesh tunnel %s: %w", name, err)
		}
		if err := attachHook(name, wireGuardProgram, "ingress"); err != nil {
			return err
		}
		tunnels.IfIndex[index] = uint32(device.Index)
	}

	return writeMap("mesh_tunnels", uint32(0), tunnels)
}

func readMeshTunnels(directory string) ([]string, error) {
	tunnelMap, err := openPinnedMap(filepath.Join(directory, "mesh_tunnels"))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer tunnelMap.Close()

	var tunnels meshTunnels
	if err := tunnelMap.Lookup(uint32(0), &tunnels); err != nil {
		return nil, err
	}

	names := make([]string, 0, tunnels.Count)
	for _, ifindex := range tunnels.IfIndex[:min(tunnels.Count, meshTunnelLimit)] {
		// Metal recreates a deleted tunnel and writes its new index at the next sync.
		if device, err := net.InterfaceByIndex(int(ifindex)); err == nil {
			names = append(names, device.Name)
		}
	}
	return names, nil
}

// readPeerState converts the peer state file into peers.
func readPeerState(path string) ([]peer, error) {
	contents, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	peers, err := decodePeerState(contents)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	return peers, nil
}

// decodePeerState reaches a peer at its private address when Atlas sends one. Atlas sends it only in a region with a private network.
func decodePeerState(contents []byte) ([]peer, error) {
	var entries []peerState
	if err := json.Unmarshal(contents, &entries); err != nil {
		return nil, err
	}

	peers := make([]peer, 0, len(entries))
	ipv4Addresses := make(map[netip.Addr]bool)
	wireGuardAddresses := make(map[netip.Addr]bool)
	macAddresses := make(map[string]bool)
	for index, entry := range entries {
		ipv4Text := entry.PublicAddress
		if entry.PrivateAddress != "" {
			ipv4Text = entry.PrivateAddress
		}

		ipv4, ipv4Error := netip.ParseAddr(ipv4Text)
		wireGuard, wireGuardError := netip.ParseAddr(entry.MeshAddress)
		mac, macError := net.ParseMAC(entry.MAC)
		if ipv4Error != nil || !ipv4.Is4() {
			return nil, fmt.Errorf("peer %d has no usable IPv4 address", index)
		}
		if wireGuardError != nil || !underlayPrefix.Contains(wireGuard) {
			return nil, fmt.Errorf("peer %d has no WireGuard address in %s", index, underlayPrefix)
		}
		if macError != nil || len(mac) != 6 || mac[0]&1 != 0 {
			return nil, fmt.Errorf("peer %d has no unicast Ethernet MAC address", index)
		}
		macText := mac.String()
		if ipv4Addresses[ipv4] || wireGuardAddresses[wireGuard] || macAddresses[macText] {
			return nil, fmt.Errorf("peer %d repeats an IPv4, WireGuard, or MAC address", index)
		}
		ipv4Addresses[ipv4] = true
		wireGuardAddresses[wireGuard] = true
		macAddresses[macText] = true

		peers = append(peers, peer{IPv4: ipv4.As4(), MAC: [6]byte(mac), WireGuardIPv6: wireGuard.As16()})
	}

	if len(peers) > peerLimit {
		return nil, fmt.Errorf("peer state holds more than %d peers", peerLimit)
	}

	return peers, nil
}

func readPeerArray() ([]peer, error) {
	values := make([]peer, peerLimit)
	for index := range peerLimit {
		value, err := readMap[peer]("peer_list", uint32(index))
		if err != nil {
			return nil, err
		}
		values[index] = value
	}
	return values, nil
}

func writePeerArray(peers []peer) error {
	for index := range peerLimit {
		var value peer
		if index < len(peers) {
			value = peers[index]
		}
		if err := writeMap("peer_list", uint32(index), value); err != nil {
			return err
		}
	}
	return nil
}

func setUnicastNDP(uplink string, enabled bool) error {
	if enabled {
		return attachHook(uplink, uplinkEgressProgram, "egress")
	}
	return detachHook(uplink, "egress")
}

// readPeers returns the loaded peers. The first empty entry ends the list.
func readPeers() ([]peer, error) {
	var peers []peer
	for index := range peerLimit {
		value, err := readMap[peer]("peer_list", uint32(index))
		if err != nil {
			return nil, err
		}
		if value.IPv4 == [4]byte{} {
			break
		}
		peers = append(peers, value)
	}

	return peers, nil
}
