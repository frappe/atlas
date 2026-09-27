package datum

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

const ingestPath = "/v1/ingest"

// Sample is one metric reading. Identity travels on the bearer token, never
// in Labels: datum stamps every row with the token's resource_id and drops
// one supplied here.
type Sample struct {
	Metric    string
	Value     float64
	Timestamp time.Time
	Labels    map[string]string
}

type wireSample struct {
	Metric    string            `json:"metric"`
	Value     float64           `json:"value"`
	Timestamp string            `json:"ts"`
	Labels    map[string]string `json:"labels,omitempty"`
}

type ingestBody struct {
	Samples []wireSample `json:"samples"`
}

// Client posts sample batches to one datum instance.
type Client struct {
	baseURL string
	http    *http.Client
}

// NewClient returns a client for the datum instance at baseURL.
func NewClient(baseURL string, timeout time.Duration) *Client {
	return &Client{baseURL: baseURL, http: &http.Client{Timeout: timeout}}
}

// Ingest sends one batch under token. Datum assigns every row to the
// resource_id the token carries.
func (c *Client) Ingest(ctx context.Context, token string, samples []Sample) error {
	wire := make([]wireSample, len(samples))
	for i, sample := range samples {
		wire[i] = wireSample{
			Metric:    sample.Metric,
			Value:     sample.Value,
			Timestamp: sample.Timestamp.UTC().Format(time.RFC3339),
			Labels:    sample.Labels,
		}
	}

	body, err := json.Marshal(ingestBody{Samples: wire})
	if err != nil {
		return fmt.Errorf("encode samples: %w", err)
	}

	request, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+ingestPath, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build ingest request: %w", err)
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+token)

	response, err := c.http.Do(request)
	if err != nil {
		return fmt.Errorf("send samples: %w", err)
	}
	defer response.Body.Close()
	// Draining lets the transport reuse this connection instead of closing it.
	_, _ = io.Copy(io.Discard, response.Body)

	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("datum returned HTTP %d", response.StatusCode)
	}
	return nil
}
