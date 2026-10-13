package platform

import (
	"context"

	systemd "github.com/coreos/go-systemd/v22/dbus"
	"github.com/godbus/dbus/v5"
)

// sshSessionUnitPrefix names the transient unit of an SSH session ID, which starts with "ssh-".
const sshSessionUnitPrefix = "metal-"

// SSHSessionUnits runs the SSH clients of console sessions as transient units.
// It shares the DBus connection.
type SSHSessionUnits struct {
	dbus *DBus
}

// SSHSessions returns the SSH session units on this connection.
func (d *DBus) SSHSessions() *SSHSessionUnits { return &SSHSessionUnits{dbus: d} }

// Start runs command in a transient unit with ttyPath as its
// controlling terminal. systemd owns the unit, so the SSH client outlives a
// metald restart. The unit is removed when the client exits.
func (units *SSHSessionUnits) Start(ctx context.Context, sessionID string, command []string, ttyPath string) error {
	properties := []systemd.Property{
		systemd.PropDescription("metal SSH console " + sessionID),
		systemd.PropExecStart(command, true),
		{Name: "Type", Value: dbus.MakeVariant("exec")},
		{Name: "StandardInput", Value: dbus.MakeVariant("tty-force")},
		{Name: "StandardOutput", Value: dbus.MakeVariant("tty")},
		{Name: "StandardError", Value: dbus.MakeVariant("tty")},
		{Name: "TTYPath", Value: dbus.MakeVariant(ttyPath)},
		{Name: "TTYReset", Value: dbus.MakeVariant(true)},
		{Name: "TTYVHangup", Value: dbus.MakeVariant(true)},
		// Without a size, systemd queries the terminal with ANSI sequences. The
		// viewer's answers would reach the guest shell as typed input. A viewer
		// resizes the terminal when it attaches.
		{Name: "TTYRows", Value: dbus.MakeVariant(uint32(24))},
		{Name: "TTYColumns", Value: dbus.MakeVariant(uint32(80))},
		// ssh forwards TERM to the guest, so it must name a terminal the guest knows.
		{Name: "Environment", Value: dbus.MakeVariant([]string{"TERM=xterm-256color"})},
		{Name: "CollectMode", Value: dbus.MakeVariant("inactive-or-failed")},
	}

	return units.dbus.runSystemdJob(ctx, "start", func(results chan<- string) error {
		_, err := units.dbus.connection.StartTransientUnitContext(ctx, sshSessionUnitName(sessionID), "fail", properties, results)
		return err
	})
}

// Stop stops the unit of an SSH session. An absent unit is already stopped.
func (units *SSHSessionUnits) Stop(ctx context.Context, sessionID string) error {
	return units.dbus.stopUnit(ctx, sshSessionUnitName(sessionID))
}

// Wait returns when the SSH client of a session exits.
func (units *SSHSessionUnits) Wait(ctx context.Context, sessionID string) error {
	return units.dbus.waitUntilStopped(ctx, sshSessionUnitName(sessionID))
}

// List returns the IDs of the SSH sessions that have a unit on this host.
func (units *SSHSessionUnits) List(ctx context.Context) ([]string, error) {
	return units.dbus.listUnitIDs(ctx, sshSessionUnitPrefix, "ssh-")
}

// sshSessionUnitName builds the transient unit name for an SSH session ID.
func sshSessionUnitName(sessionID string) string {
	return sshSessionUnitPrefix + sessionID + unitSuffix
}
