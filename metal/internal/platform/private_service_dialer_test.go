package platform

import (
	"context"
	"errors"
	"net/netip"
	"testing"
)

func TestPrivateServiceDialerUsesOnlyPrivateAnswers(t *testing.T) {
	answers := map[string][]netip.Addr{
		"internal-s3.example.com":    {netip.MustParseAddr("203.0.113.7"), netip.MustParseAddr("fdaa:1::7")},
		"internal-proxy.example.com": {netip.MustParseAddr("203.0.113.7")},
	}
	var lookedUp []string
	dialer := &PrivateServiceDialer{lookup: func(_ context.Context, _, host string) ([]netip.Addr, error) {
		lookedUp = append(lookedUp, host)
		if addresses, ok := answers[host]; ok {
			return addresses, nil
		}
		return nil, errors.New("no such host")
	}}

	for host, want := range map[string]string{
		"s3.example.com":    "fdaa:1::7",
		"proxy.example.com": "",
		"datum.example.com": "",
		"203.0.113.9":       "",
	} {
		got := ""
		if address, ok := dialer.privateAddress(context.Background(), host); ok {
			got = address.String()
		}
		if got != want {
			t.Errorf("privateAddress(%q) = %q, want %q", host, got, want)
		}
	}

	if len(lookedUp) != 3 {
		t.Errorf("lookups = %v, want none for an IP address", lookedUp)
	}
}
