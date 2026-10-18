package firecracker

import (
	"context"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
	"github.com/frappe/atlas/metal/internal/vm"
)

const (
	reachableTimeout = 3 * time.Minute
	pingInterval     = 250 * time.Millisecond
)

// The probe ends at reachableTimeout; daemon shutdown does not wait for it.
func (runtime *Runtime) recordBoot(input vm.RuntimeMachine, kind string, startedAt time.Time) {
	runtime.telemetry.Observe("metal_vm_boot_seconds", time.Since(startedAt).Seconds(), telemetry.Labels{"kind": kind, "stage": "started"})
	go runtime.waitUntilReachable(input, kind, startedAt)
}

func (runtime *Runtime) waitUntilReachable(input vm.RuntimeMachine, kind string, startedAt time.Time) {
	ctx, cancel := context.WithTimeout(context.Background(), reachableTimeout)
	defer cancel()

	ticker := time.NewTicker(pingInterval)
	defer ticker.Stop()
	for {
		if err := ping(input.NetworkInterface.NetworkNamespacePath, input.NetworkInterface.GuestIPAddress); err == nil {
			runtime.telemetry.Observe("metal_vm_boot_seconds", time.Since(startedAt).Seconds(), telemetry.Labels{"kind": kind, "stage": "reachable"})
			return
		}
		select {
		case <-ctx.Done():
			runtime.telemetry.Count("metal_vm_boot_unreachable_total", 1, telemetry.Labels{"kind": kind})
			runtime.logger.Warn("guest did not answer ping after boot", "virtual_machine_id", input.ID, "kind", kind, "timeout", reachableTimeout)
			return
		case <-ticker.C:
		}
	}
}
