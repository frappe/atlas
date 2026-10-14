package main

import (
	"log/slog"

	"github.com/frappe/atlas/metal/internal/api"
	"github.com/frappe/atlas/metal/internal/firecracker"
	platform "github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/telemetry"
	"github.com/frappe/atlas/metal/internal/telemetry/system"
	"github.com/frappe/atlas/metal/internal/vm"
	"github.com/frappe/atlas/metal/internal/vm/migration"
)

type telemetrySources struct {
	virtualMachines *vm.Manager
	migrations      *migration.Manager
	runtime         *firecracker.Runtime
}

func newTelemetryPusher(
	options options,
	logger *slog.Logger,
	registry *telemetry.Registry,
	units *platform.DBus,
	versions api.Versions,
	sources telemetrySources,
) *telemetry.Pusher {
	registry.Register("process", telemetry.NewProcessCollector(telemetry.Labels{
		"version": versions.MetaldVersion, "firecracker_version": versions.FirecrackerVersion, "mesh_version": versions.MeshVersion,
	}))
	registry.Register("virtual_machines", telemetry.CollectorFunc(sources.virtualMachines.CollectTelemetry))
	registry.Register("migrations", telemetry.CollectorFunc(sources.migrations.CollectTelemetry))
	registry.Register("runtime", telemetry.CollectorFunc(sources.runtime.CollectTelemetry))
	system.Register(registry, system.Config{Pool: options.pool, RestartCountUnits: options.telemetry.restartCountUnits, Systemd: units})

	client := telemetry.NewClient(options.configFile)
	return telemetry.NewPusher(registry, client, options.telemetry.interval, logger)
}
