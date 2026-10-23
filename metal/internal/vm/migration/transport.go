// Package migration carries VM migration traffic between Metal hosts. The
// destination host drives the migration through the mutual-TLS coordination API.
package migration

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"

	"github.com/frappe/atlas/metal/internal/vm"
)

const (
	defaultControlTimeout = 60 * time.Second
	defaultStopTimeout    = 2 * defaultControlTimeout
)

// SourceClient calls the source coordination API over mutual TLS.
type SourceClient struct {
	client         *http.Client
	stopClient     *http.Client
	transferClient *http.Client
}

// NewSourceClient returns a client with the given per-call control timeout.
func NewSourceClient(timeout time.Duration, tlsConfiguration *tls.Config) *SourceClient {
	stopTimeout := timeout
	if timeout <= 0 {
		timeout = defaultControlTimeout
		stopTimeout = defaultStopTimeout
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = tlsConfiguration
	return &SourceClient{
		client:         &http.Client{Timeout: timeout, Transport: transport},
		stopClient:     &http.Client{Timeout: stopTimeout, Transport: transport.Clone()},
		transferClient: &http.Client{Timeout: maxTransferDuration, Transport: transport.Clone()},
	}
}

// NextSnapshot acknowledges a sequence and asks for the next snapshot.
func (c *SourceClient) NextSnapshot(ctx context.Context, address, migrationID, virtualMachineID string, receivedSequence int) (SourceSnapshot, error) {
	body, err := json.Marshal(SnapshotAcknowledgement{ReceivedSequence: receivedSequence})
	if err != nil {
		return SourceSnapshot{}, err
	}
	responseBody, err := c.postJSON(ctx, c.client, address, sourcePath(migrationID, virtualMachineID, "/snapshot"), body)
	if err != nil {
		return SourceSnapshot{}, err
	}
	return decodeSnapshot(responseBody, "snapshot")
}

// StartSnapshotStream asks the source to start its one-shot snapshot listener.
func (c *SourceClient) StartSnapshotStream(ctx context.Context, address, migrationID, virtualMachineID string, sequence int, resumeToken string, throughputMiBps int) error {
	body, err := json.Marshal(SnapshotStreamRequest{
		Sequence: sequence, ResumeToken: resumeToken, ThroughputMiBps: throughputMiBps,
	})
	if err != nil {
		return err
	}
	_, err = c.postJSON(ctx, c.client, address, sourcePath(migrationID, virtualMachineID, "/stream"), body)
	return err
}

func (c *SourceClient) postJSON(ctx context.Context, client *http.Client, address, path string, body []byte) ([]byte, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, address+path, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("build source request: %w", err)
	}
	request.Header.Set("Content-Type", "application/json")
	return c.do(request, client)
}

// PrepareSource asks the source to lock the VM and return its definition and state.
func (c *SourceClient) PrepareSource(ctx context.Context, address, migrationID, virtualMachineID string) (VirtualMachineDefinition, vm.State, error) {
	body, err := c.call(ctx, http.MethodPut, address, sourcePath(migrationID, virtualMachineID, "/source")+"&sleep_state=true")
	if err != nil {
		return VirtualMachineDefinition{}, "", err
	}
	var response SourceDescription
	if err := json.Unmarshal(body, &response); err != nil {
		return VirtualMachineDefinition{}, "", fmt.Errorf("decode source description: %w", err)
	}
	return response.Definition, response.ObservedState, nil
}

// StopSource stops the VM and returns its final snapshot.
func (c *SourceClient) StopSource(ctx context.Context, address, migrationID, virtualMachineID string, receivedSequence int, hasMemorySnapshot bool) (SourceSnapshot, error) {
	body, err := json.Marshal(SnapshotAcknowledgement{ReceivedSequence: receivedSequence, HasMemorySnapshot: hasMemorySnapshot})
	if err != nil {
		return SourceSnapshot{}, err
	}
	responseBody, err := c.postJSON(ctx, c.stopClient, address, sourcePath(migrationID, virtualMachineID, "/stop"), body)
	if err != nil {
		return SourceSnapshot{}, err
	}
	return decodeSnapshot(responseBody, "stop")
}

func decodeSnapshot(body []byte, operation string) (SourceSnapshot, error) {
	var response SourceSnapshot
	if err := json.Unmarshal(body, &response); err != nil {
		return SourceSnapshot{}, fmt.Errorf("decode %s response: %w", operation, err)
	}
	return response, nil
}

// StartSource asks the source to restore its original desired state.
func (c *SourceClient) StartSource(ctx context.Context, address, migrationID, virtualMachineID string) error {
	_, err := c.call(ctx, http.MethodPost, address, sourcePath(migrationID, virtualMachineID, "/start"))
	return err
}

// FinishSource asks the source to destroy the stopped VM and state.
func (c *SourceClient) FinishSource(ctx context.Context, address, migrationID, virtualMachineID string) error {
	_, err := c.call(ctx, http.MethodPost, address, sourcePath(migrationID, virtualMachineID, "/destroy"))
	return err
}

// RemoveSource asks the source to unlock the VM and drop migration state.
func (c *SourceClient) RemoveSource(ctx context.Context, address, migrationID, virtualMachineID string) error {
	_, err := c.call(ctx, http.MethodDelete, address, sourcePath(migrationID, virtualMachineID, ""))
	return err
}

func (c *SourceClient) call(ctx context.Context, method, address, path string) ([]byte, error) {
	request, err := http.NewRequestWithContext(ctx, method, address+path, nil)
	if err != nil {
		return nil, fmt.Errorf("build source request: %w", err)
	}
	return c.do(request, c.client)
}

func (c *SourceClient) do(request *http.Request, client *http.Client) ([]byte, error) {
	response, err := client.Do(request)
	if err != nil {
		return nil, fmt.Errorf("call source: %w", err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return nil, fmt.Errorf("read source response: %w", err)
	}
	if response.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("source %s %s: %w", request.Method, request.URL.Path, vm.ErrNotFound)
	}
	if response.StatusCode == http.StatusConflict {
		return nil, fmt.Errorf("source %s %s: %w", request.Method, request.URL.Path, vm.ErrConflict)
	}
	if response.StatusCode == http.StatusUnprocessableEntity {
		var failure struct {
			Error struct {
				Code string `json:"code"`
			} `json:"error"`
		}
		cause := ErrNoMemoryToSave
		if json.Unmarshal(body, &failure) == nil && failure.Error.Code == "source_woke" {
			cause = ErrSourceWoke
		}
		return nil, fmt.Errorf("source %s %s: %w", request.Method, request.URL.Path, cause)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("source %s %s returned HTTP %d", request.Method, request.URL.Path, response.StatusCode)
	}
	return body, nil
}

func sourcePath(migrationID, virtualMachineID, suffix string) string {
	path := "/v1/migrations/" + url.PathEscape(migrationID) + suffix
	query := url.Values{"virtual_machine_id": {virtualMachineID}}
	return path + "?" + query.Encode()
}
