package vm

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"slices"
)

// wireGuardGatewayRoutesFileName holds the controller-owned WireGuard gateway
// return routes. Record listing skips plain files in the machines directory.
const wireGuardGatewayRoutesFileName = "wireguard-gateway-routes.json"

// SetWireGuardGatewayRoutes replaces the complete WireGuard gateway return
// route set. Each opted-in VM applies it on its next network pass.
func (manager *Manager) SetWireGuardGatewayRoutes(ctx context.Context, routes []Route) error {
	if err := ctx.Err(); err != nil {
		return err
	}

	manager.wireGuardGatewayRoutesMutex.Lock()
	defer manager.wireGuardGatewayRoutesMutex.Unlock()
	if slices.Equal(manager.wireGuardGatewayRoutes, routes) {
		return nil
	}
	if err := writeRecord(manager.wireGuardGatewayRoutesPath(), routes); err != nil {
		return fmt.Errorf("store WireGuard gateway routes: %w", err)
	}
	manager.wireGuardGatewayRoutes = slices.Clone(routes)
	return nil
}

// loadWireGuardGatewayRoutes restores the last route set, so a restart keeps
// the return routes before the next controller sync.
func (manager *Manager) loadWireGuardGatewayRoutes() error {
	var routes []Route
	err := readRecord(manager.wireGuardGatewayRoutesPath(), &routes)
	if err != nil && !errors.Is(err, ErrNotFound) {
		return err
	}
	manager.wireGuardGatewayRoutes = routes
	return nil
}

func (manager *Manager) wireGuardGatewayRoutesPath() string {
	return filepath.Join(manager.configuration.MachinesDirectory, wireGuardGatewayRoutesFileName)
}

// networkRequest builds the complete desired host network state for a record.
// An opted-in VM also receives the current WireGuard gateway return routes.
func (manager *Manager) networkRequest(record DesiredRecord) NetworkRequest {
	configuration := record.Specification.Network
	if configuration.IsAccessibleViaWireGuardGateway {
		configuration.Routes = withGatewayRoutes(configuration.Routes, manager.currentWireGuardGatewayRoutes())
	}

	return NetworkRequest{
		VirtualMachineID: record.ID,
		UserID:           record.UserID,
		GroupID:          record.GroupID,
		Configuration:    configuration,
		TrackTraffic:     record.State == StateRunning && record.Specification.SleepAfterIdleSeconds > 0,
	}
}

func (manager *Manager) currentWireGuardGatewayRoutes() []Route {
	manager.wireGuardGatewayRoutesMutex.RLock()
	defer manager.wireGuardGatewayRoutesMutex.RUnlock()
	return slices.Clone(manager.wireGuardGatewayRoutes)
}

// withGatewayRoutes adds the gateway routes to the VM routes. A VM route keeps
// its destination, because the operator set it, and WG Mesh refuses a repeated destination.
func withGatewayRoutes(routes, gatewayRoutes []Route) []Route {
	combined := slices.Clone(routes)
	for _, gatewayRoute := range gatewayRoutes {
		if !slices.ContainsFunc(routes, func(route Route) bool { return route.Destination == gatewayRoute.Destination }) {
			combined = append(combined, gatewayRoute)
		}
	}
	return combined
}
