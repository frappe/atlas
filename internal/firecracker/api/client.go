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
