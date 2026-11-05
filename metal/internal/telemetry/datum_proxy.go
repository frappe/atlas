package telemetry

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"time"

	"github.com/frappe/atlas/metal/internal/platform"
)

// DatumProxyAddress is the local HTTPS proxy that Vector uses to reach Datum.
// install-telemetry.sh sets the same address.
const DatumProxyAddress = "127.0.0.1:9003"

// Vector sends one request at a time, so a few tunnels are enough.
const maximumDatumTunnels = 5

// DatumProxy tunnels local CONNECT requests to Datum through the private service
// dialer, so Vector uses the same private path and fallback as metald. TLS stays
// end to end between Vector and Datum.
type DatumProxy struct {
	configFile string
	dialer     *platform.PrivateServiceDialer
	tunnels    chan struct{}
	logger     *slog.Logger
}

// NewDatumProxy returns a proxy that allows only the Datum host in configFile.
func NewDatumProxy(configFile string, logger *slog.Logger) *DatumProxy {
	dialer := net.Dialer{Timeout: requestTimeout, KeepAlive: 30 * time.Second}
	return &DatumProxy{
		configFile: configFile,
		dialer:     platform.NewPrivateServiceDialer(dialer, logger),
		tunnels:    make(chan struct{}, maximumDatumTunnels),
		logger:     logger,
	}
}

// Run serves the proxy until ctx ends. Telemetry is not critical, so a listen
// failure is logged and does not stop metald.
func (proxy *DatumProxy) Run(ctx context.Context) {
	server := &http.Server{Addr: DatumProxyAddress, Handler: proxy, ReadHeaderTimeout: requestTimeout}
	go func() {
		<-ctx.Done()
		server.Close()
	}()

	if err := server.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
		proxy.logger.Error("Datum proxy stopped", "error", err)
	}
}

// ServeHTTP opens one tunnel to Datum.
func (proxy *DatumProxy) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodConnect || !proxy.isDatum(request.Host) {
		http.Error(writer, "only CONNECT to Datum is allowed", http.StatusForbidden)
		return
	}

	select {
	case proxy.tunnels <- struct{}{}:
		defer func() { <-proxy.tunnels }()
	default:
		http.Error(writer, "too many Datum tunnels", http.StatusServiceUnavailable)
		return
	}

	upstream, err := proxy.dialer.DialContext(request.Context(), "tcp", request.Host)
	if err != nil {
		http.Error(writer, err.Error(), http.StatusBadGateway)
		return
	}
	defer upstream.Close()

	client, _, err := http.NewResponseController(writer).Hijack()
	if err != nil {
		return
	}
	defer client.Close()

	if _, err := io.WriteString(client, "HTTP/1.1 200 Connection Established\r\n\r\n"); err != nil {
		return
	}

	// Copying between two TCP connections uses splice, so bytes stay in the kernel.
	done := make(chan struct{}, 2)
	go func() { io.Copy(upstream, client); done <- struct{}{} }()
	go func() { io.Copy(client, upstream); done <- struct{}{} }()
	<-done
}

// isDatum reads the config on each tunnel, so a new Datum URL needs no restart.
func (proxy *DatumProxy) isDatum(target string) bool {
	endpoint, _, err := readCredentials(proxy.configFile)
	if err != nil {
		return false
	}

	datum, err := url.Parse(endpoint)
	if err != nil || datum.Scheme != "https" {
		return false
	}

	port := datum.Port()
	if port == "" {
		port = "443"
	}
	return target == net.JoinHostPort(datum.Hostname(), port)
}
