package platform

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"syscall"
	"time"

	systemd "github.com/coreos/go-systemd/v22/dbus"
	"github.com/godbus/dbus/v5"
)

const (
	unitPrefix = "metal-vm@"
	unitSuffix = ".service"

	// errorNoSuchUnit is the D-Bus error name systemd returns for an absent unit.
	errorNoSuchUnit = "org.freedesktop.systemd1.NoSuchUnit"

	// microsecondsPerCPUMillicore converts a millicore to the microseconds of CPU
	// time per second that systemd expects in CPUQuotaPerSecUSec.
	microsecondsPerCPUMillicore = 1000

	// waitPollInterval is how often Wait re-reads the unit state.
	waitPollInterval = 500 * time.Millisecond
)

// DBus is a UnitManager backed by systemd's D-Bus API.
type DBus struct {
	connection *systemd.Conn
}

var _ UnitManager = (*DBus)(nil)

// Connect opens a connection to the system systemd service.
func Connect(ctx context.Context) (*DBus, error) {
	connection, err := systemd.NewSystemConnectionContext(ctx)
	if err != nil {
		return nil, err
	}

	return &DBus{connection: connection}, nil
}

// Close releases the system bus connection.
func (d *DBus) Close() { d.connection.Close() }

// Start activates the unit for id and waits for the systemd job to finish.
func (d *DBus) Start(ctx context.Context, id string) error {
	return d.runSystemdJob(ctx, "start", func(results chan<- string) error {
		_, err := d.connection.StartUnitContext(ctx, unitName(id), "replace", results)
		return err
	})
}

// Stop deactivates the unit for id. An absent unit is already stopped.
func (d *DBus) Stop(ctx context.Context, id string) error {
	err := d.runSystemdJob(ctx, "stop", func(results chan<- string) error {
		_, err := d.connection.StopUnitContext(ctx, unitName(id), "replace", results)
		return err
	})
	if isUnitNotLoaded(err) {
		return nil
	}

	return err
}

// Kill sends signal to every process in the unit for id.
func (d *DBus) Kill(ctx context.Context, id string, signal syscall.Signal) error {
	return d.connection.KillUnitWithTarget(ctx, unitName(id), systemd.All, int32(signal))
}

// ResetFailed clears a unit's failed state. It succeeds when the unit is absent.
func (d *DBus) ResetFailed(ctx context.Context, id string) error {
	err := d.connection.ResetFailedUnitContext(ctx, unitName(id))
	if isUnitNotLoaded(err) {
		return nil
	}

	return err
}

// Status reports the activation state of the unit for id. An absent unit reads
// as inactive, because a virtual machine that never started is a stopped one.
func (d *DBus) Status(ctx context.Context, id string) (Status, error) {
	unit := unitName(id)

	properties, err := d.connection.GetUnitPropertiesContext(ctx, unit)
	if isUnitNotLoaded(err) {
		return Status{ActiveState: "inactive", SubState: "dead"}, nil
	}
	if err != nil {
		return Status{}, err
	}

	serviceProperties, err := d.connection.GetUnitTypePropertiesContext(ctx, unit, "Service")
	if err != nil {
		return Status{}, err
	}

	return Status{
		PID:         int(asUint32(serviceProperties["MainPID"])),
		ActiveState: asString(properties["ActiveState"]),
		SubState:    asString(properties["SubState"]),
	}, nil
}

// Wait polls the unit for id until it stops and reports how it stopped.
func (d *DBus) Wait(ctx context.Context, id string) (Result, error) {
	unit := unitName(id)
	ticker := time.NewTicker(waitPollInterval)
	defer ticker.Stop()

	for {
		properties, err := d.connection.GetUnitPropertiesContext(ctx, unit)
		if err != nil {
			return Result{}, err
		}
		switch asString(properties["ActiveState"]) {
		case "inactive", "failed":
			return d.stopResult(ctx, unit)
		}

		select {
		case <-ctx.Done():
			return Result{}, ctx.Err()
		case <-ticker.C:
		}
	}
}

// List returns the virtual machine IDs that have a unit on this host.
func (d *DBus) List(ctx context.Context) ([]string, error) {
	pattern := unitPrefix + "*" + unitSuffix
	units, err := d.connection.ListUnitsByPatternsContext(ctx, nil, []string{pattern})
	if err != nil {
		return nil, err
	}

	ids := make([]string, 0, len(units))
	for _, unit := range units {
		ids = append(ids, idFromUnit(unit.Name))
	}

	return ids, nil
}

// SetLimits applies limits to the running unit for id. Zero values are skipped,
// so a caller can raise one limit without clearing the other.
func (d *DBus) SetLimits(ctx context.Context, id string, limits Limits) error {
	var properties []systemd.Property
	if limits.MemoryMaxBytes > 0 {
		value := dbus.MakeVariant(uint64(limits.MemoryMaxBytes))
		properties = append(properties, systemd.Property{Name: "MemoryMax", Value: value})
	}
	if limits.CPUMillicores > 0 {
		quota := cpuQuotaMicrosecondsPerSecond(limits.CPUMillicores)
		value := dbus.MakeVariant(quota)
		properties = append(properties, systemd.Property{Name: "CPUQuotaPerSecUSec", Value: value})
	}
	if len(properties) == 0 {
		return nil
	}

	return d.connection.SetUnitPropertiesContext(ctx, unitName(id), true, properties...)
}

// Usage reads the unit's current cgroup memory and CPU use for id. An absent
// unit reads as a zero Usage.
func (d *DBus) Usage(ctx context.Context, id string) (Usage, error) {
	unit := unitName(id)

	// ControlGroup is a Service-type property, like MainPID, not a generic Unit
	// property.
	serviceProperties, err := d.connection.GetUnitTypePropertiesContext(ctx, unit, "Service")
	if isUnitNotLoaded(err) {
		return Usage{}, nil
	}
	if err != nil {
		return Usage{}, err
	}

	controlGroup := asString(serviceProperties["ControlGroup"])
	if controlGroup == "" {
		return Usage{}, nil
	}
	return readUsage(controlGroup)
}

func cpuQuotaMicrosecondsPerSecond(cpuMillicores int) uint64 {
	return uint64(cpuMillicores) * microsecondsPerCPUMillicore
}

// runSystemdJob submits a job and waits for its result.
func (d *DBus) runSystemdJob(ctx context.Context, operation string, submit func(chan<- string) error) error {
	results := make(chan string, 1)
	if err := submit(results); err != nil {
		return err
	}

	select {
	case result := <-results:
		if result != "done" {
			return fmt.Errorf("systemd: %s: %s", operation, result)
		}
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// stopResult reads the exit code or terminating signal of a stopped unit.
func (d *DBus) stopResult(ctx context.Context, unit string) (Result, error) {
	serviceProperties, err := d.connection.GetUnitTypePropertiesContext(ctx, unit, "Service")
	if err != nil {
		return Result{}, err
	}

	status := asInt32(serviceProperties["ExecMainStatus"])
	if asInt32(serviceProperties["ExecMainCode"]) == 1 { // CLD_EXITED
		return Result{Code: int(status)}, nil
	}

	return Result{Signal: syscall.Signal(status).String()}, nil
}

// isUnitNotLoaded reports whether systemd says the unit is absent. Some replies
// carry the message text without the typed D-Bus error name, so both are checked.
func isUnitNotLoaded(err error) bool {
	if err == nil {
		return false
	}

	var dbusError dbus.Error
	if errors.As(err, &dbusError) && dbusError.Name == errorNoSuchUnit {
		return true
	}

	return strings.Contains(err.Error(), "not loaded")
}

// unitName builds the template instance name for a virtual machine ID.
func unitName(id string) string { return unitPrefix + id + unitSuffix }

// idFromUnit recovers the virtual machine ID from a template instance name.
func idFromUnit(name string) string {
	return strings.TrimSuffix(strings.TrimPrefix(name, unitPrefix), unitSuffix)
}

// asString reads a D-Bus property as a string and returns "" for other types.
func asString(value any) string { text, _ := value.(string); return text }

// asUint32 reads a D-Bus property as a uint32 and returns 0 for other types.
func asUint32(value any) uint32 { number, _ := value.(uint32); return number }

// asInt32 reads a D-Bus property as an int32 and returns 0 for other types.
func asInt32(value any) int32 { number, _ := value.(int32); return number }
