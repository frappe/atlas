// Package api is a hand-rolled client for Firecracker's REST API, served over a
// Unix-domain socket. No external deps: net/http with a socket dialer.
package api

import (
	"bytes"
	"context"
	"encoding/json"
	"net"
	"net/http"
)

type Client struct {
	http *http.Client
}

// New binds a Client to the firecracker API socket at sockPath.
func New(sockPath string) *Client {
	return &Client{http: &http.Client{Transport: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "unix", sockPath)
		},
	}}}
}

func (c *Client) PutMachineConfig(ctx context.Context, m MachineConfig) error {
	return c.do(ctx, http.MethodPut, "/machine-config", m, nil)
}

func (c *Client) PutBootSource(ctx context.Context, b BootSource) error {
	return c.do(ctx, http.MethodPut, "/boot-source", b, nil)
}

func (c *Client) PutDrive(ctx context.Context, d Drive) error {
	return c.do(ctx, http.MethodPut, "/drives/"+d.DriveID, d, nil)
}

// PatchDrive updates a live drive; used after a host-side resize so firecracker
// rescans the block device and the guest sees the new size.
func (c *Client) PatchDrive(ctx context.Context, d PartialDrive) error {
	return c.do(ctx, http.MethodPatch, "/drives/"+d.DriveID, d, nil)
}

func (c *Client) PutNetworkInterface(ctx context.Context, n NetworkInterface) error {
	return c.do(ctx, http.MethodPut, "/network-interfaces/"+n.IfaceID, n, nil)
}

func (c *Client) InstanceStart(ctx context.Context) error {
	return c.do(ctx, http.MethodPut, "/actions", action{ActionType: "InstanceStart"}, nil)
}

func (c *Client) InstanceInfo(ctx context.Context) (InstanceInfo, error) {
	var ii InstanceInfo
	err := c.do(ctx, http.MethodGet, "/", nil, &ii)
	return ii, err
}

func (c *Client) SendCtrlAltDel(ctx context.Context) error {
	return c.do(ctx, http.MethodPut, "/actions", action{ActionType: "SendCtrlAltDel"}, nil)
}

// Pause moves the microVM to the Paused state, which halts vCPU execution.
// Firecracker requires the Paused state before it creates a snapshot.
func (c *Client) Pause(ctx context.Context) error {
	return c.do(ctx, http.MethodPatch, "/vm", vmState{State: "Paused"}, nil)
}

// Resume moves the microVM back to the Running state after a Pause or a
// snapshot load.
func (c *Client) Resume(ctx context.Context) error {
	return c.do(ctx, http.MethodPatch, "/vm", vmState{State: "Resumed"}, nil)
}

// CreateSnapshot writes a snapshot of the paused microVM to the chroot-relative
// paths in r. The VM must be Paused first.
func (c *Client) CreateSnapshot(ctx context.Context, r CreateSnapshotReq) error {
	return c.do(ctx, http.MethodPut, "/snapshot/create", r, nil)
}

// LoadSnapshot restores a microVM from a snapshot into a fresh firecracker
// process. Set r.ResumeVM to resume at once, or keep it false to stay Paused.
func (c *Client) LoadSnapshot(ctx context.Context, r LoadSnapshotReq) error {
	return c.do(ctx, http.MethodPut, "/snapshot/load", r, nil)
}

func (c *Client) PutMmdsConfig(ctx context.Context, cfg MmdsConfig) error {
	return c.do(ctx, http.MethodPut, "/mmds/config", cfg, nil)
}

// PutMmds sets the metadata payload the guest reads from the metadata service.
func (c *Client) PutMmds(ctx context.Context, data any) error {
	return c.do(ctx, http.MethodPut, "/mmds", data, nil)
}

// do sends one request to the socket. The URL host is ignored; the dialer
// always connects to the socket. body/out are JSON-encoded/decoded if non-nil.
func (c *Client) do(ctx context.Context, method, path string, body, out any) error {
	var buf bytes.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		buf.Reset(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, "http://localhost"+path, &buf)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return decodeFault(resp)
	}
	if out != nil {
		return json.NewDecoder(resp.Body).Decode(out)
	}
	return nil
}
