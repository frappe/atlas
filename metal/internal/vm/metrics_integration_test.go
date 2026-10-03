//go:build linux && integration

package vm

import (
	"fmt"
	"os"
	"os/exec"
	"testing"

	"github.com/frappe/atlas/metal/internal/network/traffic"
)

// TestMetricsKeepsNetworkCountersAcrossStopStart uses a real traffic attachment
// with the fake guest runtime so it needs no guest image or ZFS pool.
func TestMetricsKeepsNetworkCountersAcrossStopStart(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("needs root")
	}
	manager, _, network, _ := newTestManager(t)
	if _, err := manager.Create(t.Context(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(t.Context(), "machine-1"); err != nil {
		t.Fatal(err)
	}
	desired, _, err := manager.newVirtualMachine("machine-1").records()
	if err != nil {
		t.Fatal(err)
	}
	namespace := fmt.Sprintf("metrics-test-%d", os.Getpid())
	run := func(args ...string) {
		t.Helper()
		if output, err := exec.CommandContext(t.Context(), "ip", args...).CombinedOutput(); err != nil {
			t.Fatalf("ip %v: %v: %s", args, err, output)
		}
	}
	run("netns", "add", namespace)
	t.Cleanup(func() { _ = exec.Command("ip", "netns", "del", namespace).Run() })
	run("-n", namespace, "tuntap", "add", "tap0", "mode", "tap")
	run("-n", namespace, "addr", "add", "172.16.0.1/24", "dev", "tap0")
	run("-n", namespace, "link", "set", "tap0", "up")
	run("-n", namespace, "neigh", "replace", "172.16.0.2", "lladdr", "06:00:ac:10:00:02", "dev", "tap0", "nud", "permanent")
	monitor, err := traffic.NewMonitor(traffic.Config{MinimumUserID: desired.UserID, MaximumUserID: desired.UserID})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = monitor.Close() })
	manager.traffic = monitor
	if err := monitor.Attach(traffic.AttachmentRequest{Target: traffic.Target{VirtualMachineID: desired.ID, UserID: desired.UserID}, NamespacePath: "/run/netns/" + namespace, InterfaceName: "tap0"}); err != nil {
		t.Fatal(err)
	}
	// No guest replies, but the outbound request crosses the monitored TAP.
	_ = exec.CommandContext(t.Context(), "ip", "netns", "exec", namespace, "ping", "-c", "1", "-W", "1", "172.16.0.2").Run()
	before, err := manager.Metrics(t.Context(), desired.ID)
	if err != nil {
		t.Fatal(err)
	}
	if before.ReceivedPackets == 0 {
		t.Fatal("test generated no monitored traffic")
	}
	for _, state := range []State{StateStopped, StateRunning} {
		if err := manager.SetPowerState(t.Context(), desired.ID, state); err != nil {
			t.Fatal(err)
		}
		if err := manager.Reconcile(t.Context(), desired.ID); err != nil {
			t.Fatal(err)
		}
		if !network.lastRequest.TrackTraffic {
			t.Fatalf("reconcile requested traffic detachment after %s", state)
		}
		after, err := manager.Metrics(t.Context(), desired.ID)
		if err != nil {
			t.Fatal(err)
		}
		if after.ReceivedPackets < before.ReceivedPackets || after.ReceivedBytes < before.ReceivedBytes {
			t.Fatalf("counters reset after %s: before=%+v after=%+v", state, before, after)
		}
	}
}
