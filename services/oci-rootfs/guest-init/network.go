package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"os"
	"slices"
	"strings"
	"unsafe"

	"golang.org/x/sys/unix"
)

const (
	guestInterface = "eth0"
	metadataRoute  = "169.254.169.254/32"
	meshGateway    = "fe80::1"
	// guestMTU must equal meshMTU in metal/internal/network/mesh.go.
	guestMTU = 1380

	guestResolvers = "nameserver 1.1.1.1\nnameserver 1.0.0.1\nnameserver 2606:4700:4700::1111\n"
)

// configureNetwork reads the address from the ip= argument that Metal sends. The
// Ubuntu kernel has no IP autoconfiguration, so it ignores this argument.
func configureNetwork() error {
	commandLine, err := os.ReadFile("/proc/cmdline")
	if err != nil {
		return err
	}
	address, gateway, prefix, err := parseIPArgument(string(commandLine))
	if err != nil {
		return err
	}
	loopback, err := net.InterfaceByName("lo")
	if err != nil {
		return err
	}
	if err := setLinkUp(loopback.Index, 0); err != nil {
		return fmt.Errorf("loopback: %w", err)
	}
	link, err := net.InterfaceByName(guestInterface)
	if err != nil {
		return err
	}
	_ = os.WriteFile("/proc/sys/net/ipv6/conf/"+guestInterface+"/accept_ra", []byte("0"), 0o644)
	if err := setLinkUp(link.Index, guestMTU); err != nil {
		return fmt.Errorf("link: %w", err)
	}
	if err := addAddress(link.Index, net.ParseIP(address), prefix); err != nil {
		return fmt.Errorf("address: %w", err)
	}
	for _, route := range []struct{ destination, gateway string }{
		{metadataRoute, ""},
		{"0.0.0.0/0", gateway},
		{"::/0", meshGateway},
	} {
		if err := replaceRoute(link.Index, route.destination, route.gateway); err != nil {
			return fmt.Errorf("route %s: %w", route.destination, err)
		}
	}
	// An image built FROM scratch can have no /etc.
	_ = os.MkdirAll("/etc", 0o755)
	_ = os.Remove("/etc/resolv.conf")
	return os.WriteFile("/etc/resolv.conf", []byte(guestResolvers), 0o644)
}

// parseIPArgument reads ip=<address>::<gateway>:<mask>::eth0:off from the kernel command line.
func parseIPArgument(commandLine string) (address, gateway string, prefix int, err error) {
	for _, argument := range strings.Fields(commandLine) {
		value, found := strings.CutPrefix(argument, "ip=")
		if !found {
			continue
		}
		fields := strings.Split(value, ":")
		if len(fields) < 4 || net.ParseIP(fields[0]) == nil || net.ParseIP(fields[2]) == nil || net.ParseIP(fields[3]) == nil {
			return "", "", 0, fmt.Errorf("invalid ip argument %q", value)
		}
		prefix, _ = net.IPMask(net.ParseIP(fields[3]).To4()).Size()
		return fields[0], fields[2], prefix, nil
	}
	return "", "", 0, errors.New("no ip argument on the kernel command line")
}

// applyMesh makes the guest mesh address and host routes equal to the metadata values.
func (metadata *metadataClient) applyMesh(address string, routes []string) error {
	if address == metadata.address && slices.Equal(routes, metadata.routes) {
		return nil
	}
	link, err := net.InterfaceByName(guestInterface)
	if err != nil {
		return err
	}
	if address != metadata.address {
		if metadata.address != "" {
			if err := deleteAddress(link.Index, net.ParseIP(metadata.address), 128); err != nil {
				return err
			}
		}
		if err := addAddress(link.Index, net.ParseIP(address), 128); err != nil {
			return err
		}
		metadata.address = address
	}
	for _, route := range metadata.routes {
		if !slices.Contains(routes, route) {
			if err := deleteRoute(link.Index, route); err != nil {
				return err
			}
		}
	}
	for _, route := range routes {
		if err := replaceRoute(link.Index, route, meshGateway); err != nil {
			return err
		}
	}
	metadata.routes = routes
	return nil
}

func setLinkUp(index int, mtu uint32) error {
	message := unix.IfInfomsg{Family: unix.AF_UNSPEC, Index: int32(index), Flags: unix.IFF_UP, Change: unix.IFF_UP}
	var attributes []netlinkAttribute
	if mtu != 0 {
		attributes = append(attributes, netlinkAttribute{unix.IFLA_MTU, binary.NativeEndian.AppendUint32(nil, mtu)})
	}
	return netlinkRequest(unix.RTM_NEWLINK, 0, structBytes(&message), attributes)
}

func addAddress(index int, address net.IP, prefix int) error {
	return changeAddress(unix.RTM_NEWADDR, unix.NLM_F_CREATE|unix.NLM_F_REPLACE, index, address, prefix)
}

// deleteAddress succeeds when the address is already gone, so a retry converges.
func deleteAddress(index int, address net.IP, prefix int) error {
	err := changeAddress(unix.RTM_DELADDR, 0, index, address, prefix)
	if errors.Is(err, unix.EADDRNOTAVAIL) {
		return nil
	}
	return err
}

func changeAddress(messageType uint16, flags uint16, index int, address net.IP, prefix int) error {
	family, data := addressFamily(address)
	message := unix.IfAddrmsg{Family: family, Prefixlen: uint8(prefix), Index: uint32(index)}
	attributes := []netlinkAttribute{
		{unix.IFA_LOCAL, data},
		{unix.IFA_ADDRESS, data},
		{unix.IFA_FLAGS, binary.NativeEndian.AppendUint32(nil, unix.IFA_F_NODAD)},
	}
	return netlinkRequest(messageType, flags, structBytes(&message), attributes)
}

func replaceRoute(index int, destination, gateway string) error {
	return changeRoute(unix.RTM_NEWROUTE, unix.NLM_F_CREATE|unix.NLM_F_REPLACE, index, destination, gateway)
}

// deleteRoute succeeds when the route is already gone, so a retry converges.
func deleteRoute(index int, destination string) error {
	err := changeRoute(unix.RTM_DELROUTE, 0, index, destination, "")
	if errors.Is(err, unix.ESRCH) {
		return nil
	}
	return err
}

func changeRoute(messageType uint16, flags uint16, index int, destination, gateway string) error {
	if !strings.Contains(destination, "/") {
		if strings.Contains(destination, ":") {
			destination += "/128"
		} else {
			destination += "/32"
		}
	}
	_, network, err := net.ParseCIDR(destination)
	if err != nil {
		return err
	}
	family, data := addressFamily(network.IP)
	prefix, _ := network.Mask.Size()
	message := unix.RtMsg{Family: family, Dst_len: uint8(prefix), Table: unix.RT_TABLE_MAIN, Protocol: unix.RTPROT_STATIC, Scope: unix.RT_SCOPE_LINK, Type: unix.RTN_UNICAST}
	attributes := []netlinkAttribute{{unix.RTA_OIF, binary.NativeEndian.AppendUint32(nil, uint32(index))}}
	if prefix > 0 {
		attributes = append(attributes, netlinkAttribute{unix.RTA_DST, data})
	}
	if gateway != "" {
		message.Scope = unix.RT_SCOPE_UNIVERSE
		_, gatewayData := addressFamily(net.ParseIP(gateway))
		attributes = append(attributes, netlinkAttribute{unix.RTA_GATEWAY, gatewayData})
	}
	return netlinkRequest(messageType, flags, structBytes(&message), attributes)
}

func addressFamily(address net.IP) (uint8, []byte) {
	if ipv4 := address.To4(); ipv4 != nil {
		return unix.AF_INET, ipv4
	}
	return unix.AF_INET6, address.To16()
}

func structBytes[T any](value *T) []byte {
	return unsafe.Slice((*byte)(unsafe.Pointer(value)), unsafe.Sizeof(*value))
}

// netlinkAttribute is one routing attribute. The image may have no ip tool, so
// guest-init speaks netlink itself.
type netlinkAttribute struct {
	kind uint16
	data []byte
}

// netlinkRequest sends one routing message and returns the error in its acknowledgement.
func netlinkRequest(messageType, flags uint16, header []byte, attributes []netlinkAttribute) error {
	socket, err := unix.Socket(unix.AF_NETLINK, unix.SOCK_RAW|unix.SOCK_CLOEXEC, unix.NETLINK_ROUTE)
	if err != nil {
		return err
	}
	defer unix.Close(socket)

	var body bytes.Buffer
	body.Write(header)
	for _, attribute := range attributes {
		length := unix.SizeofRtAttr + len(attribute.data)
		_ = binary.Write(&body, binary.NativeEndian, unix.RtAttr{Len: uint16(length), Type: attribute.kind})
		body.Write(attribute.data)
		body.Write(make([]byte, (4-length%4)%4))
	}
	messageHeader := unix.NlMsghdr{
		Len:   uint32(unix.SizeofNlMsghdr + body.Len()),
		Type:  messageType,
		Flags: unix.NLM_F_REQUEST | unix.NLM_F_ACK | flags,
		Seq:   1,
	}
	request := append(append([]byte{}, structBytes(&messageHeader)...), body.Bytes()...)
	if err := unix.Sendto(socket, request, 0, &unix.SockaddrNetlink{Family: unix.AF_NETLINK}); err != nil {
		return err
	}

	response := make([]byte, 4096)
	length, _, err := unix.Recvfrom(socket, response, 0)
	if err != nil {
		return err
	}
	if length < unix.SizeofNlMsghdr+4 {
		return errors.New("short netlink response")
	}
	if code := int32(binary.NativeEndian.Uint32(response[unix.SizeofNlMsghdr:])); code != 0 {
		return unix.Errno(-code)
	}
	return nil
}
