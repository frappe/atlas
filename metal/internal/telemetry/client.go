package telemetry

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"strings"
	"time"

	"github.com/BurntSushi/toml"
)

// maximumBatch is the most samples Datum accepts in one request.
const maximumBatch = 10_000

const requestTimeout = 10 * time.Second

// credentialsLifetime is how long the client keeps the Datum URL and token before
// it reads the config again. Atlas renews a 7-day token daily.
const credentialsLifetime = 24 * time.Hour

// ErrNotConfigured means the host has no Datum URL and token yet.
var ErrNotConfigured = errors.New("telemetry is not configured")

// Client posts samples to Datum. It keeps the URL and token from the config file
// for 24 hours, or until the token expires, so a renewed token needs no restart.
// One goroutine calls Send.
type Client struct {
	configFile string
	httpClient *http.Client
	endpoint   string
	token      string
	refreshAt  time.Time
}

// NewClient returns a client that reads the Datum URL and token from configFile.
func NewClient(configFile string) *Client {
	return &Client{configFile: configFile, httpClient: &http.Client{Timeout: requestTimeout}}
}

// Send posts samples in batches that Datum accepts.
func (client *Client) Send(ctx context.Context, samples []Sample) error {
	endpoint, token, err := client.credentials()
	if err != nil {
		return err
	}

	for start := 0; start < len(samples); start += maximumBatch {
		batch := samples[start:min(start+maximumBatch, len(samples))]
		if err := client.post(ctx, endpoint+"/v1/ingest", token, batch); err != nil {
			return err
		}
	}
	return nil
}

func (client *Client) post(ctx context.Context, url, token string, samples []Sample) error {
	body, err := json.Marshal(struct {
		Samples []Sample `json:"samples"`
	}{samples})
	if err != nil {
		return fmt.Errorf("encode samples: %w", err)
	}

	request, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build Datum request: %w", err)
	}
	request.Header.Set("Authorization", "Bearer "+token)
	request.Header.Set("Content-Type", "application/json")

	response, err := client.httpClient.Do(request)
	if err != nil {
		return fmt.Errorf("post samples to Datum: %w", err)
	}
	defer response.Body.Close()

	if response.StatusCode/100 != 2 {
		detail, _ := io.ReadAll(io.LimitReader(response.Body, 512))
		return fmt.Errorf("datum returned HTTP %d: %s", response.StatusCode, strings.TrimSpace(string(detail)))
	}
	return nil
}

// credentials returns the kept URL and token, and reads them again when they are due.
func (client *Client) credentials() (string, string, error) {
	now := time.Now()
	if client.token != "" && now.Before(client.refreshAt) {
		return client.endpoint, client.token, nil
	}

	endpoint, token, err := readCredentials(client.configFile)
	if err != nil {
		return "", "", err
	}
	client.endpoint, client.token = endpoint, token
	client.refreshAt = now.Add(credentialsLifetime)
	if expiry, ok := tokenExpiry(token); ok && expiry.Before(client.refreshAt) {
		client.refreshAt = expiry
	}
	return endpoint, token, nil
}

// readCredentials reads datum_url and datum_token from [telemetry] in the metald config.
func readCredentials(configFile string) (string, string, error) {
	var config struct {
		Telemetry struct {
			DatumURL   string `toml:"datum_url"`
			DatumToken string `toml:"datum_token"`
		} `toml:"telemetry"`
	}
	_, err := toml.DecodeFile(configFile, &config)
	if errors.Is(err, fs.ErrNotExist) {
		return "", "", ErrNotConfigured
	}
	if err != nil {
		return "", "", fmt.Errorf("read telemetry config: %w", err)
	}

	endpoint, token := strings.TrimRight(config.Telemetry.DatumURL, "/"), config.Telemetry.DatumToken
	if endpoint == "" || token == "" {
		return "", "", ErrNotConfigured
	}
	return endpoint, token, nil
}

// tokenExpiry reads the exp claim of a JWT without verifying it. Datum verifies the token.
func tokenExpiry(token string) (time.Time, bool) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return time.Time{}, false
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return time.Time{}, false
	}

	var claims struct {
		Expiry int64 `json:"exp"`
	}
	if json.Unmarshal(payload, &claims) != nil || claims.Expiry == 0 {
		return time.Time{}, false
	}
	return time.Unix(claims.Expiry, 0), true
}
