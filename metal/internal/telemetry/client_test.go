package telemetry

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

func writeConfig(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "metald.toml")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestClientWithoutCredentialsIsNotConfigured(t *testing.T) {
	client := NewClient(writeConfig(t, "[telemetry]\ninterval = \"30s\"\n"))
	if err := client.Send(context.Background(), []Sample{{Metric: "system_cpu_count"}}); !errors.Is(err, ErrNotConfigured) {
		t.Fatalf("err = %v, want ErrNotConfigured", err)
	}
}

func TestClientSplitsSamplesIntoBatchesDatumAccepts(t *testing.T) {
	var mutex sync.Mutex
	var batchSizes []int
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/v1/ingest" || request.Header.Get("Authorization") != "Bearer token-1" {
			writer.WriteHeader(http.StatusUnauthorized)
			return
		}
		var body struct{ Samples []Sample }
		_ = json.NewDecoder(request.Body).Decode(&body)
		mutex.Lock()
		batchSizes = append(batchSizes, len(body.Samples))
		mutex.Unlock()
	}))
	defer server.Close()

	client := NewClient(writeConfig(t, "[telemetry]\ndatum_url = \""+server.URL+"/\"\ndatum_token = \"token-1\"\n"))
	samples := make([]Sample, maximumBatch+1)
	for index := range samples {
		samples[index] = Sample{Metric: "system_cpu_count", Value: 1, Time: time.Now()}
	}
	if err := client.Send(context.Background(), samples); err != nil {
		t.Fatal(err)
	}
	if len(batchSizes) != 2 || batchSizes[0] != maximumBatch || batchSizes[1] != 1 {
		t.Fatalf("batch sizes = %v, want [%d 1]", batchSizes, maximumBatch)
	}
}

func TestPusherKeepsObservationsWhenDatumRejectsThem(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer server.Close()

	registry := newTestRegistry()
	registry.Observe("metal_vm_action_duration_seconds", 2.5, Labels{"action": "create"})
	client := NewClient(writeConfig(t, "[telemetry]\ndatum_url = \""+server.URL+"\"\ndatum_token = \"token-1\"\n"))
	NewPusher(registry, client, time.Second, registry.logger).Push(context.Background(), time.Now())

	observations := registry.TakeObservations()
	if len(observations) != 1 || observations[0].Value != 2.5 {
		t.Fatalf("observations = %+v, want the rejected observation kept", observations)
	}
}

func signedToken(expiry time.Time) string {
	payload, _ := json.Marshal(map[string]int64{"exp": expiry.Unix()})
	return "header." + base64.RawURLEncoding.EncodeToString(payload) + ".signature"
}

func TestClientKeepsTheTokenUntilItExpires(t *testing.T) {
	var sent []string
	server := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, request *http.Request) {
		sent = append(sent, strings.TrimPrefix(request.Header.Get("Authorization"), "Bearer "))
	}))
	defer server.Close()

	config := filepath.Join(t.TempDir(), "metald.toml")
	writeToken := func(token string) {
		content := "[telemetry]\ndatum_url = \"" + server.URL + "\"\ndatum_token = \"" + token + "\"\n"
		if err := os.WriteFile(config, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	send := func(client *Client) {
		if err := client.Send(context.Background(), []Sample{{Metric: "system_cpu_count", Value: 1, Time: time.Now()}}); err != nil {
			t.Fatal(err)
		}
	}
	valid, expired, renewed := signedToken(time.Now().Add(time.Hour)), signedToken(time.Now().Add(-time.Minute)), signedToken(time.Now().Add(2*time.Hour))

	writeToken(valid)
	kept := NewClient(config)
	send(kept)
	writeToken(renewed)
	send(kept)

	writeToken(expired)
	refreshed := NewClient(config)
	send(refreshed)
	writeToken(renewed)
	send(refreshed)

	want := []string{valid, valid, expired, renewed}
	if !slices.Equal(sent, want) {
		t.Fatalf("sent tokens = %v, want %v", sent, want)
	}
}
