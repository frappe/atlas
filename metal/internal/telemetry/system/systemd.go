package system

import (
	"context"
	"strings"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

type systemdCollector struct {
	restartCountUnits []string
	reader            UnitReader
}

// Collect reports unit states and restarts, aggregating temporary units.
func (collector systemdCollector) Collect(ctx context.Context) ([]telemetry.Sample, error) {
	units, err := collector.reader.UnitStates(ctx, []string{"*"})
	if err != nil {
		return nil, err
	}

	var samples []telemetry.Sample
	counts := make(map[string]float64)
	virtualMachines := make(map[string]float64)
	for _, unit := range units {
		counts[unit.ActiveState]++
		switch {
		case strings.HasPrefix(unit.Name, "metal-vm@"):
			virtualMachines[unit.ActiveState]++
		case isTransientUnit(unit.Name):
		default:
			samples = append(samples, gauge("system_systemd_unit_state", 1, telemetry.Labels{"unit": unit.Name, "state": unit.ActiveState, "sub_state": unit.SubState}))
		}
	}
	for state, count := range counts {
		samples = append(samples, gauge("system_systemd_units", count, telemetry.Labels{"state": state}))
	}
	for state, count := range virtualMachines {
		samples = append(samples, gauge("system_systemd_virtual_machine_units", count, telemetry.Labels{"state": state}))
	}
	samples = append(samples, gauge("system_systemd_failed_units", counts["failed"], nil))

	restarts, err := collector.reader.UnitRestarts(ctx, collector.restartCountUnits)
	if err != nil {
		return samples, err
	}
	for unit, count := range restarts {
		samples = append(samples, gauge("system_systemd_unit_restarts_total", float64(count), telemetry.Labels{"unit": unit}))
	}
	return samples, nil
}

func isTransientUnit(name string) bool {
	return strings.HasSuffix(name, ".scope") || strings.HasPrefix(name, "run-") ||
		strings.HasPrefix(name, "metal-ssh") || strings.HasPrefix(name, "user@") ||
		strings.HasPrefix(name, "user-runtime-dir@") || strings.HasPrefix(name, "session-")
}
