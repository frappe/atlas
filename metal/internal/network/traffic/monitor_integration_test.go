//go:build linux && integration

package traffic

import (
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"testing"
	"time"
	"unsafe"

	"github.com/frappe/atlas/metal/internal/platform"
	"golang.org/x/sys/unix"
)

const (
	testTapName         = "tap0"
	testGuestIPAddress  = "172.16.0.2"
	testGuestMACAddress = "06:00:ac:10:00:02"
)

type interfaceRequest struct {
	name  [16]byte
	flags uint16
	_     [22]byte
}

// TestMonitorObservesPacketsWithoutAGuest proves the traffic behavior needed for idle shutdown.
//
//	sudo -E go test -tags integration -run TestMonitorObservesPacketsWithoutAGuest ./internal/network/traffic/
func TestMonitorObservesPacketsWithoutAGuest(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("needs root")
	}

	namespace := fmt.Sprintf("metal-traffic-test-%d", os.Getpid())
	namespacePath := "/run/netns/" + namespace
	const userID = 100001
	target := Target{VirtualMachineID: "vm-traffic-test", UserID: userID}

	runOrSkip(t, "ip", "netns", "add", namespace)
	t.Cleanup(func() { _ = runQuietly("ip", "netns", "del", namespace) })
	runInNetworkNamespaceOrSkip(t, namespace, "ip", "tuntap", "add", testTapName, "mode", "tap")
	runInNetworkNamespaceOrSkip(t, namespace, "ip", "addr", "add", "172.16.0.1/24", "dev", testTapName)
	runInNetworkNamespaceOrSkip(t, namespace, "ip", "link", "set", testTapName, "up")
	runInNetworkNamespaceOrSkip(t, namespace, "ip", "link", "set", "lo", "up")
	runInNetworkNamespaceOrSkip(t, namespace, "ip", "neigh", "replace", testGuestIPAddress, "lladdr", testGuestMACAddress, "dev", testTapName, "nud", "permanent")

	monitor, err := NewMonitor(Config{MinimumUserID: userID, MaximumUserID: userID})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = monitor.Close() })
	if err := monitor.Attach(AttachmentRequest{
		Target: target, NamespacePath: namespacePath, InterfaceName: testTapName,
	}); err != nil {
		t.Fatal(err)
	}

	baseline := sampleTraffic(t, monitor, target)
	sendHostPacket(t, namespacePath, "udp")
	withoutReader := waitForNewTraffic(t, monitor, target, baseline.PacketSequence)
	assertReceivedCounterAdvanced(t, monitor, target, TrafficCounters{})

	tapQueue := openTapQueue(t, namespacePath)
	t.Cleanup(func() { _ = unix.Close(tapQueue) })
	if _, err := unix.Write(tapQueue, ipv4Frame(6)); err != nil {
		t.Fatal(err)
	}
	assertTrafficUnchanged(t, monitor, target, withoutReader.PacketSequence)
	assertSentCounterAdvanced(t, monitor, target, TrafficCounters{})

	sendHostPacket(t, namespacePath, "tcp")
	waitForNewTraffic(t, monitor, target, withoutReader.PacketSequence)
}

// assertReceivedCounterAdvanced waits for the received counter to advance.
func assertReceivedCounterAdvanced(t *testing.T, monitor *Monitor, target Target, previous TrafficCounters) TrafficCounters {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		received, _, err := monitor.Counters(target)
		if err != nil {
			t.Fatal(err)
		}
		if received.Packets > previous.Packets {
			return received
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("received counter did not advance")
	return TrafficCounters{}
}

// assertSentCounterAdvanced waits for the sent counter to advance.
func assertSentCounterAdvanced(t *testing.T, monitor *Monitor, target Target, previous TrafficCounters) TrafficCounters {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		_, sent, err := monitor.Counters(target)
		if err != nil {
			t.Fatal(err)
		}
		if sent.Packets > previous.Packets {
			return sent
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("sent counter did not advance")
	return TrafficCounters{}
}

func sampleTraffic(t *testing.T, monitor *Monitor, target Target) Sample {
	t.Helper()
	sample, err := monitor.Sample(target)
	if err != nil {
		t.Fatal(err)
	}
	return sample
}

func waitForNewTraffic(t *testing.T, monitor *Monitor, target Target, previousSequence uint64) Sample {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		sample := sampleTraffic(t, monitor, target)
		if sample.PacketSequence > previousSequence {
			return sample
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("traffic did not advance")
	return Sample{}
}

func assertTrafficUnchanged(t *testing.T, monitor *Monitor, target Target, packetSequence uint64) {
	t.Helper()
	time.Sleep(300 * time.Millisecond)
	if sample := sampleTraffic(t, monitor, target); sample.PacketSequence != packetSequence {
		t.Fatal("guest traffic changed the host-to-guest sample")
	}
}

func sendHostPacket(t *testing.T, namespacePath, transport string) {
	t.Helper()
	err := withNetworkNamespace(namespacePath, func() error {
		address := net.JoinHostPort(testGuestIPAddress, "9")
		connection, err := net.DialTimeout(transport, address, 300*time.Millisecond)
		if transport == "tcp" {
			if connection != nil {
				_ = connection.Close()
			}
			return nil
		}
		if err != nil {
			return err
		}
		defer connection.Close()
		_, err = connection.Write([]byte{0})
		return err
	})
	if err != nil {
		t.Fatalf("send %s packet: %v", transport, err)
	}
}

func openTapQueue(t *testing.T, namespacePath string) int {
	t.Helper()
	fileDescriptor := -1
	err := withNetworkNamespace(namespacePath, func() error {
		descriptor, err := unix.Open("/dev/net/tun", unix.O_RDWR|unix.O_CLOEXEC, 0)
		if err != nil {
			return err
		}
		request := interfaceRequest{flags: unix.IFF_TAP | unix.IFF_NO_PI}
		copy(request.name[:], testTapName)
		if _, _, errno := unix.Syscall(unix.SYS_IOCTL, uintptr(descriptor), uintptr(unix.TUNSETIFF), uintptr(unsafe.Pointer(&request))); errno != 0 {
			_ = unix.Close(descriptor)
			return errno
		}
		fileDescriptor = descriptor
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return fileDescriptor
}

func ipv4Frame(protocol byte) []byte {
	header := make([]byte, 20)
	header[0] = 0x45
	header[9] = protocol
	return append([]byte{
		0x06, 0x00, 0xac, 0x10, 0x00, 0x02,
		0x06, 0x00, 0xac, 0x10, 0x00, 0x01,
		0x08, 0x00,
	}, header...)
}

func runInNetworkNamespaceOrSkip(t *testing.T, namespace, name string, arguments ...string) {
	t.Helper()
	if output, err := platform.RunInNetworkNamespace(t.Context(), namespace, name, arguments...); err != nil {
		t.Skipf("namespace setup failed: %v: %s", err, output)
	}
}

func runOrSkip(t *testing.T, name string, arguments ...string) {
	t.Helper()
	commandContext, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	if output, err := exec.CommandContext(commandContext, name, arguments...).CombinedOutput(); err != nil {
		t.Skipf("setup command %s failed: %v: %s", name, err, output)
	}
}

func runQuietly(name string, arguments ...string) error {
	commandContext, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return exec.CommandContext(commandContext, name, arguments...).Run()
}
