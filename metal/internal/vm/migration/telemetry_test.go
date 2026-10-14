package migration

import (
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

func TestTransitionsObserveDowntimeAndOutcome(t *testing.T) {
	registry := telemetry.NewRegistry(slog.New(slog.NewTextHandler(io.Discard, nil)))
	now := time.Date(2026, 10, 14, 0, 0, 0, 0, time.UTC)
	manager := &Manager{
		logger: slog.New(slog.NewTextHandler(io.Discard, nil)), telemetry: registry,
		now: func() time.Time { return now }, downtimeStarts: make(map[string]time.Time),
	}
	record := destinationRecord{ID: "migration-1", VirtualMachineID: "vm-1-0001", CreatedAt: now, State: destinationCopying}
	record.Intervals = []TransferProgress{{BytesTransferred: 300}, {BytesTransferred: 200}}

	stopping := record
	stopping.State = destinationStopping
	manager.recordTransition(record, stopping)

	now = now.Add(3 * time.Second)
	ready := stopping
	ready.State = destinationReady
	manager.recordTransition(stopping, ready)

	now = now.Add(time.Minute)
	manager.recordTransition(ready, terminalDestinationRecord(ready, destinationCompleted, now))

	values := make(map[string]float64)
	for _, observation := range registry.TakeObservations() {
		values[observation.Metric] = observation.Value
	}
	if values["metal_migration_downtime_seconds"] != 3 {
		t.Fatalf("downtime = %v, want 3", values["metal_migration_downtime_seconds"])
	}
	if values["metal_migration_duration_seconds"] != 63 || values["metal_migration_transferred_bytes"] != 500 {
		t.Fatalf("outcome = %v, want 63 seconds and 500 bytes", values)
	}
}
