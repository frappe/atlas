package platform

import (
	"context"
	"log/slog"
	"net"
	"net/netip"
)

// PrivateServiceDialer dials a regional service at the private address that Atlas
// publishes as the internal-<host> DNS record. The HTTP request keeps the original
// host, so TLS verification and presigned URL signatures do not change.
type PrivateServiceDialer struct {
	dialer net.Dialer
	lookup func(ctx context.Context, network, host string) ([]netip.Addr, error)
	logger *slog.Logger
}

// NewPrivateServiceDialer returns a dialer that uses dialer for every connection.
func NewPrivateServiceDialer(dialer net.Dialer, logger *slog.Logger) *PrivateServiceDialer {
	return &PrivateServiceDialer{dialer: dialer, lookup: net.DefaultResolver.LookupNetIP, logger: logger}
}

// DialContext dials the private address of the host when it has one. It dials the
// given address when the host has none or the private dial fails.
func (d *PrivateServiceDialer) DialContext(ctx context.Context, network, address string) (net.Conn, error) {
	host, port, err := net.SplitHostPort(address)
	if err != nil {
		return nil, err
	}

	if private, ok := d.privateAddress(ctx, host); ok {
		connection, err := d.dialer.DialContext(ctx, network, net.JoinHostPort(private.String(), port))
		if err == nil {
			return connection, nil
		}
		d.logger.Info("private service dial failed, using the public address", "host", host, "address", private, "error", err)
	}

	return d.dialer.DialContext(ctx, network, address)
}

// privateAddress ignores public answers because the Atlas wildcard record answers
// every unknown name with the public proxy address.
func (d *PrivateServiceDialer) privateAddress(ctx context.Context, host string) (netip.Addr, bool) {
	if net.ParseIP(host) != nil {
		return netip.Addr{}, false
	}

	addresses, err := d.lookup(ctx, "ip", "internal-"+host)
	if err != nil {
		return netip.Addr{}, false
	}

	for _, address := range addresses {
		if address = address.Unmap(); address.IsPrivate() {
			return address, true
		}
	}
	return netip.Addr{}, false
}
