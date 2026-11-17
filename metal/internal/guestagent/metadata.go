// Package guestagent is the atlas-guest-agent binary that runs inside each VM.
package guestagent

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

const (
	metadataBaseURL         = "http://169.254.169.254/latest/"
	metadataTokenTTLSeconds = "21600"
	metadataTimeout         = 3 * time.Second
	metadataPollInterval    = 250 * time.Millisecond
	// A smaller gap is left to time sync, which slews the clock instead of stepping it.
	clockStepThreshold = 2 * time.Second
)

// RunMetadata applies the per-VM metadata that Metal writes to MMDS until ctx ends.
func RunMetadata(ctx context.Context) {
	client := newMetadataClient()
	ticker := time.NewTicker(metadataPollInterval)
	defer ticker.Stop()
	for {
		applyMetadata(ctx, client)

		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// WriteAuthorizedKeys writes each SSH public key from MMDS, one per line.
func WriteAuthorizedKeys(ctx context.Context, writer io.Writer) error {
	client := newMetadataClient()
	entries, err := client.read(ctx, "meta-data/public-keys/")
	if err != nil {
		return err
	}

	// An entry is "<index>/" for a nested key, or "<index>=<name>" in the EC2 format.
	for entry := range strings.FieldsSeq(entries) {
		index, _, _ := strings.Cut(strings.TrimSuffix(entry, "/"), "=")
		key, err := client.read(ctx, "meta-data/public-keys/"+index+"/openssh-key")
		if err != nil {
			return err
		}
		if _, err := fmt.Fprintln(writer, key); err != nil {
			return err
		}
	}
	return nil
}

func applyMetadata(ctx context.Context, client *metadataClient) {
	hostname, hostnameError := client.read(ctx, "meta-data/local-hostname")
	address, addressError := client.read(ctx, "meta-data/mesh-ipv6")
	if hostnameError != nil || addressError != nil {
		return
	}

	// First, so the network services that the identity change restarts read the right time.
	if hostTime, err := client.read(ctx, "meta-data/host-time-unix-ms"); err == nil {
		applyHostTime(hostTime)
	}
	if hostname != "" {
		applyHostname(ctx, hostname)
	}

	routes, routesError := client.read(ctx, "meta-data/host-routes")
	// Metal before the mesh MTU setting has no key, so the image MTU stays.
	mtu, mtuError := client.read(ctx, "meta-data/mesh-mtu")
	// A failed read keeps the current network, because an empty value would remove routes or the MTU.
	if address == "" || routesError != nil || mtuError != nil {
		return
	}
	mtuBytes, err := strconv.Atoi(mtu)
	if mtu != "" && err != nil {
		return
	}
	routeList := strings.FieldsFunc(routes, func(character rune) bool { return character == ',' || character == ' ' })
	applyMeshNetwork(ctx, address, routeList, mtuBytes)
}

// applyHostTime steps the clock forward to the host time that Metal sets before a
// memory restore, because a restored guest keeps its snapshot clock. It never steps back.
func applyHostTime(value string) {
	milliseconds, err := strconv.ParseInt(value, 10, 64)
	if err != nil {
		return
	}
	hostTime := time.UnixMilli(milliseconds)
	if time.Until(hostTime) <= clockStepThreshold {
		return
	}
	timespec := unix.NsecToTimespec(hostTime.UnixNano())
	_ = unix.ClockSettime(unix.CLOCK_REALTIME, &timespec)
}

func applyHostname(ctx context.Context, hostname string) {
	if current, err := os.Hostname(); err == nil && current == hostname {
		return
	}
	// A failure before D-Bus starts is retried on the next poll.
	_ = runCommand(ctx, "hostnamectl", "set-hostname", hostname)
}

// metadataClient reads MMDS with one session token.
type metadataClient struct {
	http  *http.Client
	token string
}

// newMetadataClient ignores proxy settings, because MMDS is only on the guest link.
func newMetadataClient() *metadataClient {
	return &metadataClient{http: &http.Client{Timeout: metadataTimeout, Transport: &http.Transport{}}}
}

// read returns the value at path, or an empty string for a missing key.
func (client *metadataClient) read(ctx context.Context, path string) (string, error) {
	if client.token == "" {
		token, err := client.request(ctx, http.MethodPut, "api/token", "X-metadata-token-ttl-seconds", metadataTokenTTLSeconds)
		if err != nil {
			return "", err
		}
		client.token = token
	}
	return client.request(ctx, http.MethodGet, path, "X-metadata-token", client.token)
}

func (client *metadataClient) request(ctx context.Context, method, path, headerName, headerValue string) (string, error) {
	request, err := http.NewRequestWithContext(ctx, method, metadataBaseURL+path, nil)
	if err != nil {
		return "", err
	}
	request.Header.Set(headerName, headerValue)

	response, err := client.http.Do(request)
	if err != nil {
		return "", err
	}
	defer response.Body.Close()

	switch response.StatusCode {
	case http.StatusOK:
		body, err := io.ReadAll(response.Body)
		return strings.TrimSpace(string(body)), err
	case http.StatusNotFound:
		return "", nil
	case http.StatusUnauthorized:
		// The token expired, or a restore or migration started a new Firecracker process.
		client.token = ""
	}
	return "", fmt.Errorf("metadata %s %s: status %d", method, path, response.StatusCode)
}
