package firecracker

import (
	"context"
	"errors"
	"net"
	"os"
	"time"

	"golang.org/x/net/icmp"
	"golang.org/x/net/ipv4"

	"github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/vm"
)

const pingTimeout = 200 * time.Millisecond

// Ping returns nil when the guest answers one ICMP echo from its network namespace.
func (runtime *Runtime) Ping(_ context.Context, input vm.RuntimeMachine) error {
	return ping(input.NetworkInterface.NetworkNamespacePath, input.NetworkInterface.GuestIPAddress)
}

func ping(namespacePath, address string) error {
	destination := net.ParseIP(address).To4()
	if destination == nil {
		return errors.New("guest address is not IPv4")
	}

	var connection *icmp.PacketConn
	err := platform.WithNetworkNamespace(namespacePath, func() error {
		var err error
		connection, err = icmp.ListenPacket("ip4:icmp", "0.0.0.0")
		return err
	})
	if err != nil {
		return err
	}
	defer connection.Close()

	identifier := os.Getpid() & 0xffff
	request, err := (&icmp.Message{
		Type: ipv4.ICMPTypeEcho,
		Body: &icmp.Echo{ID: identifier, Seq: 1, Data: []byte("atlas")},
	}).Marshal(nil)
	if err != nil {
		return err
	}
	if _, err := connection.WriteTo(request, &net.IPAddr{IP: destination}); err != nil {
		return err
	}
	if err := connection.SetReadDeadline(time.Now().Add(pingTimeout)); err != nil {
		return err
	}

	reply := make([]byte, 1500)
	for {
		size, peer, err := connection.ReadFrom(reply)
		if err != nil {
			return err
		}
		message, err := icmp.ParseMessage(1, reply[:size])
		peerAddress, isIPAddress := peer.(*net.IPAddr)
		if err != nil || message.Type != ipv4.ICMPTypeEchoReply || !isIPAddress || !peerAddress.IP.Equal(destination) {
			continue
		}
		if echo, ok := message.Body.(*icmp.Echo); ok && echo.ID == identifier {
			return nil
		}
	}
}
