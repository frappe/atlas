// Package api provides a Firecracker API client for a Unix socket.
package api

import (
	"bytes"
	"context"
	"encoding/json"
	"net"
	"net/http"
	"sync"
)

var socketLocks sync.Map

// Client sends requests to one Firecracker API socket.
type Client struct {
	httpClient *http.Client
	lock       *sync.Mutex
}

// New returns a client for one Firecracker API socket.
func New(socketPath string) *Client {
	lock, _ := socketLocks.LoadOrStore(socketPath, &sync.Mutex{})
	return &Client{
		lock: lock.(*sync.Mutex),
		httpClient: &http.Client{Transport: &http.Transport{
			DisableKeepAlives: true,
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				var dialer net.Dialer
				return dialer.DialContext(ctx, "unix", socketPath)
			},
		}},
	}
}

// PutMachineConfig sets compute resources.
func (client *Client) PutMachineConfig(ctx context.Context, configuration MachineConfig) error {
	return client.send(ctx, http.MethodPut, "/machine-config", configuration, nil)
}

// PutBootSource sets the kernel and boot arguments.
func (client *Client) PutBootSource(ctx context.Context, source BootSource) error {
	return client.send(ctx, http.MethodPut, "/boot-source", source, nil)
}

// PutDrive adds one drive.
func (client *Client) PutDrive(ctx context.Context, drive Drive) error {
	return client.send(ctx, http.MethodPut, "/drives/"+drive.DriveID, drive, nil)
}

// PatchDrive rescans a live drive after a resize.
func (client *Client) PatchDrive(ctx context.Context, drive PartialDrive) error {
	return client.send(ctx, http.MethodPatch, "/drives/"+drive.DriveID, drive, nil)
}

// PutNetworkInterface adds one network interface.
func (client *Client) PutNetworkInterface(ctx context.Context, networkInterface NetworkInterface) error {
	return client.send(
		ctx,
		http.MethodPut,
		"/network-interfaces/"+networkInterface.IfaceID,
		networkInterface,
		nil,
	)
}

// InstanceStart starts the virtual machine.
func (client *Client) InstanceStart(ctx context.Context) error {
	return client.send(ctx, http.MethodPut, "/actions", action{ActionType: "InstanceStart"}, nil)
}

// InstanceInfo returns the Firecracker process state.
func (client *Client) InstanceInfo(ctx context.Context) (InstanceInfo, error) {
	var information InstanceInfo
	err := client.send(ctx, http.MethodGet, "/", nil, &information)
	return information, err
}

// SendCtrlAltDel requests a guest shutdown.
func (client *Client) SendCtrlAltDel(ctx context.Context) error {
	return client.send(ctx, http.MethodPut, "/actions", action{ActionType: "SendCtrlAltDel"}, nil)
}

// Pause pauses virtual CPU execution.
func (client *Client) Pause(ctx context.Context) error {
	return client.send(ctx, http.MethodPatch, "/vm", virtualMachineState{State: "Paused"}, nil)
}

// Resume resumes virtual CPU execution.
func (client *Client) Resume(ctx context.Context) error {
	return client.send(ctx, http.MethodPatch, "/vm", virtualMachineState{State: "Resumed"}, nil)
}

// CreateSnapshot writes a snapshot to the requested paths.
func (client *Client) CreateSnapshot(ctx context.Context, request CreateSnapshotRequest) error {
	return client.send(ctx, http.MethodPut, "/snapshot/create", request, nil)
}

// LoadSnapshot restores a snapshot into a new process.
func (client *Client) LoadSnapshot(ctx context.Context, request LoadSnapshotRequest) error {
	return client.send(ctx, http.MethodPut, "/snapshot/load", request, nil)
}

// PutMMDSConfig configures the metadata service.
func (client *Client) PutMMDSConfig(ctx context.Context, configuration MMDSConfig) error {
	return client.send(ctx, http.MethodPut, "/mmds/config", configuration, nil)
}

// PutMMDS sets guest metadata.
func (client *Client) PutMMDS(ctx context.Context, data any) error {
	return client.send(ctx, http.MethodPut, "/mmds", data, nil)
}

func (client *Client) send(ctx context.Context, method, path string, body, output any) error {
	client.lock.Lock()
	defer client.lock.Unlock()

	var requestBody bytes.Reader
	if body != nil {
		encodedBody, err := json.Marshal(body)
		if err != nil {
			return err
		}
		requestBody.Reset(encodedBody)
	}

	request, err := http.NewRequestWithContext(ctx, method, "http://localhost"+path, &requestBody)
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")

	response, err := client.httpClient.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()

	if response.StatusCode >= http.StatusMultipleChoices {
		return decodeFault(response)
	}
	if output != nil {
		return json.NewDecoder(response.Body).Decode(output)
	}

	return nil
}
