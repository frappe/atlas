package vm

import (
	"slices"
	"testing"
)

func TestOnlyAnOptedInVMReceivesTheWireGuardGatewayRoutes(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	gatewayRoute := Route{Destination: "fdac:1:1::/48", Via: "fdaa:1::1"}
	if err := manager.SetWireGuardGatewayRoutes(t.Context(), []Route{gatewayRoute}); err != nil {
		t.Fatal(err)
	}

	ownRoute := Route{Destination: "0.0.0.0/0", Via: RouteViaHost}
	record := DesiredRecord{ID: "vm-1", Specification: Specification{Network: NetworkConfiguration{Routes: []Route{ownRoute}}}}
	if routes := manager.networkRequest(record).Configuration.Routes; !slices.Equal(routes, []Route{ownRoute}) {
		t.Fatalf("routes without opt-in = %v", routes)
	}

	record.Specification.Network.IsAccessibleViaWireGuardGateway = true
	if routes := manager.networkRequest(record).Configuration.Routes; !slices.Equal(routes, []Route{ownRoute, gatewayRoute}) {
		t.Fatalf("routes with opt-in = %v", routes)
	}
	if !slices.Equal(record.Specification.Network.Routes, []Route{ownRoute}) {
		t.Fatalf("the stored specification changed: %v", record.Specification.Network.Routes)
	}
}

func TestWireGuardGatewayRoutesSurviveARestart(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	routes := []Route{{Destination: "fdac:1:1::/48", Via: "fdaa:1::1"}}
	if err := manager.SetWireGuardGatewayRoutes(t.Context(), routes); err != nil {
		t.Fatal(err)
	}

	restarted, err := NewManager(manager.configuration, ManagerDependencies{
		Runtime: manager.runtime, Network: manager.network, Storage: manager.storage, Snapshots: manager.snapshots,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(restarted.wireGuardGatewayRoutes, routes) {
		t.Fatalf("routes after restart = %v", restarted.wireGuardGatewayRoutes)
	}
}

func TestAVMRouteKeepsItsDestinationOverAGatewayRoute(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	gatewayRoutes := []Route{
		{Destination: "fdac:1:1::/48", Via: "fdaa:1::1"},
		{Destination: "fdac:1:2::/48", Via: "fdaa:1::2"},
	}
	if err := manager.SetWireGuardGatewayRoutes(t.Context(), gatewayRoutes); err != nil {
		t.Fatal(err)
	}

	ownRoute := Route{Destination: "fdac:1:1::/48", Via: "fdaa:1::99"}
	record := DesiredRecord{ID: "vm-1", Specification: Specification{Network: NetworkConfiguration{
		Routes:                          []Route{ownRoute},
		IsAccessibleViaWireGuardGateway: true,
	}}}

	routes := manager.networkRequest(record).Configuration.Routes
	if !slices.Equal(routes, []Route{ownRoute, gatewayRoutes[1]}) {
		t.Fatalf("routes = %v", routes)
	}
}

func TestRescueDisablesIdleTrafficMonitoringAndKeepsGatewayRoutes(t *testing.T) {
	manager := &Manager{wireGuardGatewayRoutes: []Route{{Destination: "fdac:1:1::/48", Via: "fdaa:1::1"}}}
	record := DesiredRecord{State: StateRunning, Specification: Specification{
		SleepAfterIdleSeconds: 60,
		Network:               NetworkConfiguration{IsAccessibleViaWireGuardGateway: true},
	}}
	if !manager.networkRequest(record).TrackTraffic {
		t.Fatal("normal idle-enabled VM must track traffic")
	}
	record.Specification.Rescue.Enabled = true
	request := manager.networkRequest(record)
	if request.TrackTraffic || !slices.Equal(request.Configuration.Routes, manager.wireGuardGatewayRoutes) {
		t.Fatalf("rescue network request = %+v", request)
	}
}
