package systemd

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"syscall"
	"time"

	sd "github.com/coreos/go-systemd/v22/dbus"
	godbus "github.com/godbus/dbus/v5"
)

const errorNoSuchUnit = "org.freedesktop.systemd1.NoSuchUnit"

const (
	unitPrefix = "metal-vm@"
	unitSuffix = ".service"
)

func unitName(id string) string { return unitPrefix + id + unitSuffix }

func idFromUnit(name string) string {
	return strings.TrimSuffix(strings.TrimPrefix(name, unitPrefix), unitSuffix)
}

// DBus is a Manager backed by systemd's D-Bus API.
type DBus struct {
	conn *sd.Conn
}

// Connect opens a connection to the system systemd instance.
func Connect(ctx context.Context) (*DBus, error) {
	c, err := sd.NewSystemConnectionContext(ctx)
	if err != nil {
		return nil, err
	}
	return &DBus{conn: c}, nil
}

func (d *DBus) Close() { d.conn.Close() }

func (d *DBus) Start(ctx context.Context, id string) error {
	return d.job(ctx, "start", func(ch chan<- string) (int, error) {
		return d.conn.StartUnitContext(ctx, unitName(id), "replace", ch)
	})
}

func (d *DBus) Stop(ctx context.Context, id string) error {
	return d.job(ctx, "stop", func(ch chan<- string) (int, error) {
		return d.conn.StopUnitContext(ctx, unitName(id), "replace", ch)
	})
}

func (d *DBus) job(ctx context.Context, verb string, start func(chan<- string) (int, error)) error {
	ch := make(chan string, 1)
	if _, err := start(ch); err != nil {
		return err
	}
	select {
	case res := <-ch:
		if res != "done" {
			return fmt.Errorf("systemd: %s: %s", verb, res)
		}
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (d *DBus) Kill(ctx context.Context, id string, sig syscall.Signal) error {
	return d.conn.KillUnitWithTarget(ctx, unitName(id), sd.All, int32(sig))
}

// ResetFailed clears a unit's failed state. It succeeds when the unit is absent.
func (d *DBus) ResetFailed(ctx context.Context, id string) error {
	err := d.conn.ResetFailedUnitContext(ctx, unitName(id))
	if isUnitNotLoaded(err) {
		return nil
	}
	return err
}

// isUnitNotLoaded reports whether systemd says the unit is absent.
func isUnitNotLoaded(err error) bool {
	if err == nil {
		return false
	}
	var dbusError godbus.Error
	if errors.As(err, &dbusError) && dbusError.Name == errorNoSuchUnit {
		return true
	}
	return strings.Contains(err.Error(), "not loaded")
}

func (d *DBus) Status(ctx context.Context, id string) (Status, error) {
	unit := unitName(id)
	props, err := d.conn.GetUnitPropertiesContext(ctx, unit)
	if err != nil {
		return Status{}, err
	}
	sprops, err := d.conn.GetUnitTypePropertiesContext(ctx, unit, "Service")
	if err != nil {
		return Status{}, err
	}
	return Status{
		PID:         int(asUint32(sprops["MainPID"])),
		ActiveState: asString(props["ActiveState"]),
		SubState:    asString(props["SubState"]),
	}, nil
}

func (d *DBus) Wait(ctx context.Context, id string) (Result, error) {
	unit := unitName(id)
	t := time.NewTicker(500 * time.Millisecond)
	defer t.Stop()
	for {
		props, err := d.conn.GetUnitPropertiesContext(ctx, unit)
		if err != nil {
			return Result{}, err
		}
		switch asString(props["ActiveState"]) {
		case "inactive", "failed":
			return d.result(ctx, unit)
		}
		select {
		case <-ctx.Done():
			return Result{}, ctx.Err()
		case <-t.C:
		}
	}
}

func (d *DBus) result(ctx context.Context, unit string) (Result, error) {
	sprops, err := d.conn.GetUnitTypePropertiesContext(ctx, unit, "Service")
	if err != nil {
		return Result{}, err
	}
	status := asInt32(sprops["ExecMainStatus"])
	if asInt32(sprops["ExecMainCode"]) == 1 { // CLD_EXITED
		return Result{Code: int(status)}, nil
	}
	return Result{Signal: syscall.Signal(status).String()}, nil
}

func (d *DBus) List(ctx context.Context) ([]string, error) {
	units, err := d.conn.ListUnitsByPatternsContext(ctx, nil, []string{unitPrefix + "*" + unitSuffix})
	if err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(units))
	for _, u := range units {
		ids = append(ids, idFromUnit(u.Name))
	}
	return ids, nil
}

func (d *DBus) SetLimits(ctx context.Context, id string, l Limits) error {
	var props []sd.Property
	if l.MemoryMaxBytes > 0 {
		props = append(props, sd.Property{Name: "MemoryMax", Value: godbus.MakeVariant(uint64(l.MemoryMaxBytes))})
	}
	if l.CPUQuotaPct > 0 {
		props = append(props, sd.Property{Name: "CPUQuotaPerSecUSec", Value: godbus.MakeVariant(uint64(l.CPUQuotaPct) * 10000)})
	}
	if len(props) == 0 {
		return nil
	}
	return d.conn.SetUnitPropertiesContext(ctx, unitName(id), true, props...)
}

func asString(v any) string { s, _ := v.(string); return s }
func asUint32(v any) uint32 { u, _ := v.(uint32); return u }
func asInt32(v any) int32   { i, _ := v.(int32); return i }

var _ Manager = (*DBus)(nil)
