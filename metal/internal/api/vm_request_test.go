package api

import (
	"fmt"
	"strings"
	"testing"

	"github.com/frappe/atlas/metal/internal/vm"
)

func TestFirewallValidationAcceptsSupportedRules(t *testing.T) {
	request := firewallRequest{
		Enabled: true,
		Inbound: []firewallRuleRequest{{
			Protocol: "tcp", Ports: "22", CIDRs: []string{"203.0.113.0/24", "2001:db8::/32"},
		}},
		Outbound: []firewallRuleRequest{{
			Protocol: "udp", Ports: "8000-9000", CIDRs: []string{"0.0.0.0/0"},
		}},
	}

	if err := request.validate(); err != nil {
		t.Fatal(err)
	}
}

func TestFirewallValidationRejectsInvalidRules(t *testing.T) {
	tests := []struct {
		name string
		rule firewallRuleRequest
		want string
	}{
		{"unknown protocol", firewallRuleRequest{Protocol: "gre", CIDRs: []string{"0.0.0.0/0"}}, "protocol"},
		{"ports on ICMP", firewallRuleRequest{Protocol: "icmp", Ports: "8", CIDRs: []string{"0.0.0.0/0"}}, "ports"},
		{"reversed ports", firewallRuleRequest{Protocol: "tcp", Ports: "100-50", CIDRs: []string{"0.0.0.0/0"}}, "ascending"},
		{"leading zero", firewallRuleRequest{Protocol: "tcp", Ports: "022", CIDRs: []string{"0.0.0.0/0"}}, "leading zeros"},
		{"missing CIDR", firewallRuleRequest{Protocol: "any"}, "cidrs"},
		{"host bits", firewallRuleRequest{Protocol: "any", CIDRs: []string{"203.0.113.7/24"}}, "canonical"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			err := (firewallRequest{Inbound: []firewallRuleRequest{test.rule}}).validate()
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("error = %v, want %q", err, test.want)
			}
		})
	}
}

func TestFirewallValidationLimitsPrefixEntries(t *testing.T) {
	cidrs := make([]string, maximumFirewallPrefixes+1)
	for index := range cidrs {
		cidrs[index] = fmt.Sprintf("10.0.0.%d/32", index)
	}
	request := firewallRequest{Inbound: []firewallRuleRequest{{Protocol: "any", CIDRs: cidrs}}}

	err := request.validate()
	if err == nil || !strings.Contains(err.Error(), "50 prefix entries") {
		t.Fatalf("error = %v", err)
	}
}

func TestRouteValidationAcceptsHostAndGatewayRoutes(t *testing.T) {
	request := networkRequest{
		WireGuardMeshIPv6: "fdaa:1::4a",
		Routes: []routeRequest{
			{Destination: "0.0.0.0/0", Via: "host"},
			{Destination: "2000::/3", Via: "fdaa:1::49"},
			{Destination: "fdac::/16", Via: "fdaa:1::7f"},
			{Destination: "fdac:1:1::/48", Via: "fdaa:1::1", Scope: vm.RouteScopeWireGuardGateway},
		},
	}
	if err := request.validateRoutes(); err != nil {
		t.Fatal(err)
	}
}

func TestRouteValidationRejectsInvalidValues(t *testing.T) {
	tests := []struct {
		name  string
		route routeRequest
		want  string
	}{
		{"public gateway", routeRequest{Destination: "::/0", Via: "2001:db8::1"}, "fdaa::/16"},
		{"unknown via", routeRequest{Destination: "0.0.0.0/0", Via: "server"}, "fdaa::/16"},
		{"IPv4 through a gateway", routeRequest{Destination: "0.0.0.0/0", Via: "fdaa:1::49"}, `via "host"`},
		{"destination with host bits", routeRequest{Destination: "fdac::5/16", Via: "fdaa:1::7f"}, "canonical"},
		{"upper case", routeRequest{Destination: "2001:DB8::/32", Via: "host"}, "canonical"},
		{"unknown scope", routeRequest{Destination: "fdac:1:1::/48", Via: "fdaa:1::1", Scope: "tenant"}, "wireguard-gateway"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			err := test.route.validate()
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("error = %v, want %q", err, test.want)
			}
		})
	}
}

func TestPublicIPv6ValidationRejectsANonCanonicalPrefix(t *testing.T) {
	hostRoute := []routeRequest{{Destination: "2000::/3", Via: "host"}}
	for _, prefix := range []string{"2001:db8::5/64", "2001:DB8::/64", "203.0.113.0/24"} {
		request := networkRequest{PublicIPv6: prefix, WireGuardMeshIPv6: "fdaa:1::49", Routes: hostRoute}
		if err := request.validatePublicAddresses(); err == nil || !strings.Contains(err.Error(), "canonical") {
			t.Fatalf("%s: error = %v", prefix, err)
		}
	}
}

func TestGuestValidationBoundsMetadataServiceValues(t *testing.T) {
	request := guestRequest{
		Hostname: strings.Repeat("h", maximumHostnameLength),
		SSHKeys:  []string{},
		UserData: strings.Repeat("u", maximumUserDataLength),
	}
	if err := request.validate(); err != nil {
		t.Fatal(err)
	}

	request.UserData += "u"
	if err := request.validate(); err == nil || !strings.Contains(err.Error(), "user_data") {
		t.Fatalf("error = %v", err)
	}
}
