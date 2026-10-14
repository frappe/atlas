package vm

import (
	"context"
	"errors"
	"os"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

const mebibyte = 1 << 20

// Routine successes are counted, but not observed, to limit sample volume.
func (manager *Manager) recordOperation(phase string, startedAt time.Time, err error) {
	result := "ok"
	if err != nil {
		result = "failed"
	}
	labels := telemetry.Labels{"phase": phase, "result": result}
	duration := time.Since(startedAt).Seconds()
	manager.telemetry.Count("metal_vm_operations_total", 1, labels)
	manager.telemetry.Count("metal_vm_operation_duration_seconds_total", duration, labels)
	if err != nil || !isRoutinePhase(phase) {
		manager.telemetry.Observe("metal_vm_operation_duration_seconds", duration, labels)
	}
}

// Request correlation is in memory; after a restart, use VM ID and generation.
func (manager *Manager) noteRequest(ctx context.Context, record DesiredRecord) {
	requestID := telemetry.RequestID(ctx)
	if requestID == "" {
		return
	}
	manager.requestsMutex.Lock()
	defer manager.requestsMutex.Unlock()
	manager.requests[record.ID] = notedRequest{id: requestID, generation: record.Generation, restartGeneration: record.RestartGeneration}
}

// Discard requests for generations applied outside reconciliation.
func (manager *Manager) takeRequest(desired DesiredRecord) string {
	manager.requestsMutex.Lock()
	defer manager.requestsMutex.Unlock()
	noted := manager.requests[desired.ID]
	delete(manager.requests, desired.ID)
	if noted.generation != desired.Generation || noted.restartGeneration != desired.RestartGeneration {
		return ""
	}
	return noted.id
}

// Action duration includes the wait between the API write and reconciliation.
func (manager *Manager) recordAction(desired DesiredRecord, before ObservedRecord, acceptedAt time.Time) {
	after, err := manager.store.readObserved(desired.ID)
	removed := errors.Is(err, ErrNotFound)
	if err != nil && !removed {
		return
	}

	action := completedAction(desired, before, after, removed)
	if action == "" {
		return
	}
	manager.telemetry.Count("metal_vm_actions_total", 1, telemetry.Labels{"action": action})
	attributes := []any{
		"component", "vm", "action", action, "virtual_machine_id", desired.ID,
		"generation", desired.Generation, "observed_state", after.State,
	}

	// The idle watcher starts a sleep, so no request waits for it.
	if action != "sleep" {
		duration := time.Since(acceptedAt)
		manager.telemetry.Observe("metal_vm_action_duration_seconds", duration.Seconds(), telemetry.Labels{"action": action})
		attributes = append(attributes, "duration", duration, "request_id", manager.takeRequest(desired))
	}
	manager.logger.Info("virtual machine action completed", attributes...)
}

func completedAction(desired DesiredRecord, before, after ObservedRecord, removed bool) string {
	switch {
	case removed:
		return "terminate"
	case before.State == StateUnknown && after.State != StateUnknown:
		return "create"
	case after.RestartGeneration > before.RestartGeneration:
		return "restart"
	case after.SpecificationGeneration > before.SpecificationGeneration:
		return "resize"
	case after.State != before.State && after.State == StateStopped && desired.State == StateRunning:
		return "sleep"
	case after.State != before.State && after.State == StateRunning:
		return "start"
	case after.State != before.State && after.State == StateStopped:
		return "stop"
	case after.State != before.State && after.State == StatePaused:
		return "pause"
	case after.Generation > before.Generation:
		return "update"
	}
	return ""
}

// The desired file modification time marks API acceptance.
func (manager *Manager) desiredWrittenAt(identifier string) time.Time {
	information, err := os.Stat(manager.store.desiredPath(identifier))
	if err != nil {
		return time.Now()
	}
	return information.ModTime()
}

type inventory struct {
	states             map[string]float64
	sleepyStates       map[string]float64
	phases             map[string]float64
	pending, failing   float64
	oldestPending      time.Duration
	cpuMillicores      float64
	memoryMiB, diskMiB float64
}

// CollectTelemetry reports VM inventory, allocations, and progress.
func (manager *Manager) CollectTelemetry(context.Context) ([]telemetry.Sample, error) {
	identifiers, err := manager.store.listIDs()
	if err != nil {
		return nil, err
	}

	counts := newInventory()
	for _, identifier := range identifiers {
		desired, observed, err := manager.newVirtualMachine(identifier).records()
		if err != nil {
			continue
		}
		manager.addToInventory(&counts, desired, observed)
	}
	return counts.samples(), nil
}

func newInventory() inventory {
	return inventory{
		states:       make(map[string]float64),
		sleepyStates: make(map[string]float64),
		phases:       make(map[string]float64),
	}
}

func (manager *Manager) addToInventory(counts *inventory, desired DesiredRecord, observed ObservedRecord) {
	state := string(observed.State)
	if desired.State == StateRunning && observed.State == StateStopped && desired.Specification.SleepAfterIdleSeconds > 0 {
		state = "sleeping"
	}
	counts.states[state]++
	if desired.Specification.SleepAfterIdleSeconds > 0 {
		counts.sleepyStates[state]++
	}
	if observed.Phase != "" {
		counts.phases[observed.Phase]++
	}
	if observed.Error != nil {
		counts.failing++
	}
	if desired.State != StateDestroyed {
		counts.cpuMillicores += float64(desired.Specification.CPUMillicores)
		counts.memoryMiB += float64(desired.Specification.MemoryMiB)
		counts.diskMiB += float64(desired.Specification.DiskMiB)
	}

	isApplied := observed.Generation == desired.Generation && observed.RestartGeneration == desired.RestartGeneration
	if !isApplied || desired.State == StateDestroyed {
		counts.pending++
		counts.oldestPending = max(counts.oldestPending, time.Since(manager.desiredWrittenAt(desired.ID)))
	}
}

func (counts inventory) samples() []telemetry.Sample {
	samples := []telemetry.Sample{
		{Metric: "metal_vm_pending", Value: counts.pending},
		{Metric: "metal_vm_oldest_pending_seconds", Value: counts.oldestPending.Seconds()},
		{Metric: "metal_vm_failing", Value: counts.failing},
		{Metric: "metal_vm_allocated_cpu_millicores", Value: counts.cpuMillicores},
		{Metric: "metal_vm_allocated_memory_bytes", Value: counts.memoryMiB * mebibyte},
		{Metric: "metal_vm_allocated_disk_bytes", Value: counts.diskMiB * mebibyte},
	}
	for state, count := range counts.states {
		samples = append(samples, telemetry.Sample{Metric: "metal_vm_count", Value: count, Labels: telemetry.Labels{"state": state}})
	}
	for state, count := range counts.sleepyStates {
		samples = append(samples, telemetry.Sample{Metric: "metal_vm_sleepy_count", Value: count, Labels: telemetry.Labels{"state": state}})
	}
	for phase, count := range counts.phases {
		samples = append(samples, telemetry.Sample{Metric: "metal_vm_operations_in_progress", Value: count, Labels: telemetry.Labels{"phase": phase}})
	}
	return samples
}
