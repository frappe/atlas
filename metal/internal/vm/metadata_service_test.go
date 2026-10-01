package vm

import (
	"errors"
	"strings"
	"testing"
)

func TestMetadataServiceSizeRejectsAnOversizedDocument(t *testing.T) {
	specification := Specification{Metadata: map[string]string{
		"key": strings.Repeat("v", MetadataServiceSizeLimitBytes-metadataServiceReserveBytes-200),
	}}
	if err := specification.validateMetadataServiceSize("vm-1"); err != nil {
		t.Fatal(err)
	}

	specification.Metadata["key"] += strings.Repeat("v", 200)
	if err := specification.validateMetadataServiceSize("vm-1"); !errors.Is(err, ErrMetadataServiceTooLarge) {
		t.Fatalf("error = %v", err)
	}
}

// A scoped route stays out of the guest metadata.
func TestMetadataServiceDataOmitsWireGuardGatewayRoutes(t *testing.T) {
	specification := Specification{Network: NetworkConfiguration{Routes: []Route{
		{Destination: "2000::/3", Via: RouteViaHost},
		{Destination: "fdac:1:1::/48", Via: "fdaa:1::1", Scope: RouteScopeWireGuardGateway},
	}}}

	metadata := specification.MetadataServiceData("vm-1", "", "")["latest"].(map[string]any)["meta-data"].(map[string]any)
	if metadata["host-routes"] != "2000::/3" {
		t.Fatalf("host routes = %v, want only the unscoped route", metadata["host-routes"])
	}
}
