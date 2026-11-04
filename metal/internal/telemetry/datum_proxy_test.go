package telemetry

import (
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
)

func TestDatumProxyTunnelsOnlyToDatum(t *testing.T) {
	datum := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.WriteHeader(http.StatusNoContent)
	}))
	defer datum.Close()
	other := httptest.NewTLSServer(http.NotFoundHandler())
	defer other.Close()

	proxy := httptest.NewServer(NewDatumProxy(writeConfig(t, "[telemetry]\ndatum_url = \""+datum.URL+"\"\ndatum_token = \"token-1\"\n"), slog.Default()))
	defer proxy.Close()
	proxyURL, _ := url.Parse(proxy.URL)
	client := datum.Client()
	client.Transport.(*http.Transport).Proxy = http.ProxyURL(proxyURL)

	response, err := client.Get(datum.URL)
	if err != nil {
		t.Fatalf("request through the proxy: %v", err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusNoContent {
		t.Fatalf("status = %d, want %d", response.StatusCode, http.StatusNoContent)
	}

	if _, err := client.Get(other.URL); err == nil {
		t.Fatal("the proxy tunneled to a host that is not Datum")
	}
}
