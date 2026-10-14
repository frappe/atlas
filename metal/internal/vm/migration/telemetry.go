package migration

import (
	"context"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

func (m *Manager) saveDestination(record destinationRecord) error {
	previous, _ := m.store.readDestination(record.VirtualMachineID)
	if err := m.store.writeDestination(record); err != nil {
		return err
	}
	if previous.State != record.State {
		m.recordTransition(previous, record)
	}
	return nil
}

func (m *Manager) recordTransition(previous, current destinationRecord) {
	now := m.now()
	attributes := []any{
		"component", "migration", "migration_id", current.ID, "virtual_machine_id", current.VirtualMachineID,
		"from", previous.State, "to", current.State, "elapsed", now.Sub(current.CreatedAt),
	}
	if current.Error != nil {
		attributes = append(attributes, "error_code", current.Error.Code, "error", current.Error.LocalDetail)
	}
	m.logger.Info("migration state changed", attributes...)
	m.telemetry.Count("metal_migration_transitions_total", 1, telemetry.Labels{"state": string(current.State)})

	switch {
	case current.State == destinationStopping:
		m.setDowntimeStart(current.ID, now)
	case current.State == destinationReady:
		if startedAt, ok := m.takeDowntimeStart(current.ID); ok {
			m.telemetry.Observe("metal_migration_downtime_seconds", now.Sub(startedAt).Seconds(), nil)
		}
	case current.State.terminal():
		m.takeDowntimeStart(current.ID)
		m.observeOutcome(previous, current, now)
	}
}

func (m *Manager) observeOutcome(previous, current destinationRecord, now time.Time) {
	var transferredBytes int64
	for _, interval := range previous.Intervals {
		transferredBytes += interval.BytesTransferred
	}
	labels := telemetry.Labels{"result": string(current.State)}
	m.telemetry.Observe("metal_migration_duration_seconds", now.Sub(current.CreatedAt).Seconds(), labels)
	m.telemetry.Observe("metal_migration_transferred_bytes", float64(transferredBytes), labels)
	m.telemetry.Observe("metal_migration_intervals", float64(len(previous.Intervals)), labels)
}

func (m *Manager) setDowntimeStart(migrationID string, startedAt time.Time) {
	m.downtimeMutex.Lock()
	defer m.downtimeMutex.Unlock()
	m.downtimeStarts[migrationID] = startedAt
}

// Downtime timing is in memory and is lost on a metald restart.
func (m *Manager) takeDowntimeStart(migrationID string) (time.Time, bool) {
	m.downtimeMutex.Lock()
	defer m.downtimeMutex.Unlock()
	startedAt, ok := m.downtimeStarts[migrationID]
	delete(m.downtimeStarts, migrationID)
	return startedAt, ok
}

// CollectTelemetry counts destination migrations by state and source locks.
func (m *Manager) CollectTelemetry(context.Context) ([]telemetry.Sample, error) {
	virtualMachineIDs, err := m.store.listVirtualMachineIDs()
	if err != nil {
		return nil, err
	}

	states := make(map[string]float64)
	sources := 0.0
	for _, virtualMachineID := range virtualMachineIDs {
		if m.store.has(m.store.sourcePath(virtualMachineID)) {
			sources++
		}
		if !m.store.has(m.store.destinationPath(virtualMachineID)) {
			continue
		}
		if record, err := m.store.readDestination(virtualMachineID); err == nil {
			states[string(record.State)]++
		}
	}

	samples := []telemetry.Sample{{Metric: "metal_migration_sources", Value: sources}}
	for state, count := range states {
		samples = append(samples, telemetry.Sample{Metric: "metal_migration_destinations", Value: count, Labels: telemetry.Labels{"state": state}})
	}
	return samples, nil
}
