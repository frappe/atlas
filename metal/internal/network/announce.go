package network

import (
	"fmt"
	"net"
	"net/netip"

	"golang.org/x/sys/unix"
)

// announcePublicIPv4 sends a gratuitous ARP reply, so the provider router learns the new host at once.
func announcePublicIPv4(interfaceName, publicIPv4 string) error {
	uplink, err := net.InterfaceByName(interfaceName)
	if err != nil {
		return fmt.Errorf("find the public interface: %w", err)
	}
	address, err := netip.ParseAddr(publicIPv4)
	if err != nil {
		return fmt.Errorf("parse the public IPv4 address: %w", err)
	}

	socket, err := unix.Socket(unix.AF_PACKET, unix.SOCK_RAW, int(htons(unix.ETH_P_ARP)))
	if err != nil {
		return fmt.Errorf("open the ARP socket: %w", err)
	}
	defer unix.Close(socket)

	target := &unix.SockaddrLinklayer{Protocol: htons(unix.ETH_P_ARP), Ifindex: uplink.Index}
	frame := gratuitousARPFrame(uplink.HardwareAddr, address.As4())
	if err := unix.Sendto(socket, frame, 0, target); err != nil {
		return fmt.Errorf("announce %s: %w", publicIPv4, err)
	}
	return nil
}

// gratuitousARPFrame builds a broadcast ARP reply in which the sender and the target are the address.
func gratuitousARPFrame(mac net.HardwareAddr, address [4]byte) []byte {
	broadcast := net.HardwareAddr{0xff, 0xff, 0xff, 0xff, 0xff, 0xff}
	frame := append([]byte{}, broadcast...)
	frame = append(frame, mac...)
	frame = append(frame, 0x08, 0x06)                // EtherType ARP
	frame = append(frame, 0, 1, 0x08, 0, 6, 4, 0, 2) // Ethernet, IPv4, address lengths, reply
	frame = append(frame, mac...)
	frame = append(frame, address[:]...)
	frame = append(frame, broadcast...)
	return append(frame, address[:]...)
}

// announcePublicIPv6 sends an unsolicited neighbor advertisement, so the provider router learns the new host at once.
// The address has no prefix length.
func announcePublicIPv6(interfaceName, publicIPv6 string) error {
	uplink, err := net.InterfaceByName(interfaceName)
	if err != nil {
		return fmt.Errorf("find the public interface: %w", err)
	}
	address, err := netip.ParseAddr(publicIPv6)
	if err != nil {
		return fmt.Errorf("parse the public IPv6 address: %w", err)
	}

	socket, err := unix.Socket(unix.AF_INET6, unix.SOCK_RAW, unix.IPPROTO_ICMPV6)
	if err != nil {
		return fmt.Errorf("open the NDP socket: %w", err)
	}
	defer unix.Close(socket)

	if err := unix.SetsockoptInt(socket, unix.IPPROTO_IPV6, unix.IPV6_MULTICAST_HOPS, 255); err != nil {
		return err
	}
	if err := unix.SetsockoptInt(socket, unix.IPPROTO_IPV6, unix.IPV6_MULTICAST_IF, uplink.Index); err != nil {
		return err
	}

	allNodes := &unix.SockaddrInet6{ZoneId: uint32(uplink.Index), Addr: [16]byte{0: 0xff, 1: 0x02, 15: 1}}
	if err := unix.Sendto(socket, neighborAdvertisement(uplink.HardwareAddr, address.As16()), 0, allNodes); err != nil {
		return fmt.Errorf("announce %s: %w", publicIPv6, err)
	}
	return nil
}

// neighborAdvertisement builds an advertisement with the override flag and the target MAC option. Linux fills the checksum.
func neighborAdvertisement(mac net.HardwareAddr, address [16]byte) []byte {
	message := []byte{136, 0, 0, 0, 0x20, 0, 0, 0}
	message = append(message, address[:]...)
	message = append(message, 2, 1)
	return append(message, mac...)
}

func htons(value uint16) uint16 {
	return value<<8 | value>>8
}
