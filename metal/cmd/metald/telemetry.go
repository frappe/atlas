package main

import (
	"log/slog"

	"github.com/frappe/atlas/metal/internal/api"
	platform "github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/telemetry"
	"github.com/frappe/atlas/metal/internal/telemetry/system"
)

func newTelemetryPusher(
	options options,
	logger *slog.Logger,
	registry *telemetry.Registry,
	units *platform.DBus,
	versions api.Versions,
) *telemetry.Pusher {
	registry.Register("process", telemetry.NewProcessCollector(telemetry.Labels{
		"version": versions.MetaldVersion, "firecracker_version": versions.FirecrackerVersion, "mesh_version": versions.MeshVersion,
	}))
	system.Register(registry, system.Config{Pool: options.pool, RestartCountUnits: options.telemetry.restartCountUnits, Systemd: units})

	client := telemetry.NewClient(options.configFile)
	return telemetry.NewPusher(registry, client, options.telemetry.interval, logger)
}
