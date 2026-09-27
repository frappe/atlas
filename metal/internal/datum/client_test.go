package datum

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestIngestSendsTheAuthorizationHeaderAndBody(t *testing.T) {
	var gotAuth, gotPath string
	var gotBody ingestBody
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotPath = r.URL.Path
		_ = json.NewDecoder(r.Body).Decode(&gotBody)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	client := NewClient(server.URL, time.Second)
	err := client.Ingest(context.Background(), "test-token", []Sample{
		{Metric: "vm_up", Value: 1, Timestamp: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)},
	})
	if err != nil {
		t.Fatal(err)
	}

	if gotPath != "/v1/ingest" {
		t.Fatalf("path = %q", gotPath)
	}
	if gotAuth != "Bearer test-token" {
		t.Fatalf("Authorization = %q", gotAuth)
	}
	if len(gotBody.Samples) != 1 || gotBody.Samples[0].Metric != "vm_up" || gotBody.Samples[0].Value != 1 {
		t.Fatalf("samples = %+v", gotBody.Samples)
	}
	if gotBody.Samples[0].Timestamp != "2026-01-01T00:00:00Z" {
		t.Fatalf("ts = %q", gotBody.Samples[0].Timestamp)
	}
}

func TestIngestReturnsAnErrorOnAFailureStatus(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer server.Close()

	client := NewClient(server.URL, time.Second)
	if err := client.Ingest(context.Background(), "bad-token", []Sample{{Metric: "vm_up", Value: 1}}); err == nil {
		t.Fatal("expected an error for a 401 response")
	}
}
