package main

import (
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	metadataBaseURL      = "http://169.254.169.254/latest/"
	metadataPollInterval = 250 * time.Millisecond
	attributesTimeout    = 3 * time.Second
	// clockStepThreshold leaves a smaller gap to the guest clock source.
	clockStepThreshold = 2 * time.Second
)

// metadataClient reads the Firecracker metadata service (MMDS). Its watch loop owns the applied network state.
type metadataClient struct {
	client  *http.Client
	token   string
	address string
	routes  []string
}

func newMetadataClient() *metadataClient {
	return &metadataClient{client: &http.Client{
		Timeout:   time.Second,
		Transport: &http.Transport{DisableKeepAlives: true},
	}}
}

// request returns one response body and treats a missing key as empty. It keeps
// the exact bytes, because a metadata attribute can hold any value.
func (metadata *metadataClient) request(method, path, token string) (string, error) {
	request, err := http.NewRequest(method, metadataBaseURL+path, nil)
	if err != nil {
		return "", err
	}
	if token == "" {
		request.Header.Set("X-metadata-token-ttl-seconds", "21600")
	} else {
		request.Header.Set("X-metadata-token", token)
	}
	response, err := metadata.client.Do(request)
	if err != nil {
		return "", err
	}
	defer response.Body.Close()

	body, err := io.ReadAll(response.Body)
	if err != nil {
		return "", err
	}
	switch response.StatusCode {
	case http.StatusOK:
		return string(body), nil
	case http.StatusNotFound:
		return "", nil
	default:
		return "", fmt.Errorf("metadata %s: status %d", path, response.StatusCode)
	}
}

// read gets one key with a new token, for callers outside the watch loop.
func (metadata *metadataClient) read(path string) (string, error) {
	token, err := metadata.request(http.MethodPut, "api/token", "")
	if err != nil {
		return "", err
	}
	return metadata.request(http.MethodGet, path, token)
}

// readValue returns one key whose value has no meaningful surrounding space.
func (metadata *metadataClient) readValue(path, token string) (string, error) {
	value, err := metadata.request(http.MethodGet, path, token)
	return strings.TrimSpace(value), err
}

// attributes returns the VM metadata map. It gives up after a short time, so that
// a stop request that arrives during the first boot is not late.
func (metadata *metadataClient) attributes() (map[string]string, error) {
	deadline := time.Now().Add(attributesTimeout)
	token, err := metadata.request(http.MethodPut, "api/token", "")
	for err != nil && time.Now().Before(deadline) {
		time.Sleep(200 * time.Millisecond)
		token, err = metadata.request(http.MethodPut, "api/token", "")
	}
	if err != nil {
		return nil, fmt.Errorf("read metadata token: %w", err)
	}
	listing, err := metadata.request(http.MethodGet, "meta-data/attributes/", token)
	if err != nil {
		return nil, fmt.Errorf("read metadata attributes: %w", err)
	}

	result := map[string]string{}
	for _, key := range strings.Fields(listing) {
		if time.Now().After(deadline) {
			return nil, errors.New("read metadata attributes: timed out")
		}
		key = strings.TrimSuffix(key, "/")
		if result[key], err = metadata.request(http.MethodGet, "meta-data/attributes/"+key, token); err != nil {
			return nil, fmt.Errorf("read metadata attribute %s: %w", key, err)
		}
	}
	return result, nil
}

// publicKeys returns the SSH public keys that are authorized now, including a console key.
func (metadata *metadataClient) publicKeys() []string {
	listing, err := metadata.read("meta-data/public-keys/")
	if err != nil {
		return nil
	}
	var keys []string
	for _, entry := range strings.Fields(listing) {
		index, _, _ := strings.Cut(strings.TrimSuffix(entry, "/"), "=")
		if key, err := metadata.read("meta-data/public-keys/" + index + "/openssh-key"); err == nil && key != "" {
			keys = append(keys, key)
		}
	}
	return keys
}

// watch applies metadata changes for the life of the guest, including after a memory restore.
func (metadata *metadataClient) watch() {
	for {
		if err := growRootFileSystem(); err != nil {
			fmt.Println("guest-init: grow file system:", err)
		}
		metadata.applyOnce()
		time.Sleep(metadataPollInterval)
	}
}

func (metadata *metadataClient) applyOnce() {
	if metadata.token == "" {
		token, err := metadata.request(http.MethodPut, "api/token", "")
		if err != nil {
			return
		}
		metadata.token = strings.TrimSpace(token)
	}

	hostname, hostnameError := metadata.readValue("meta-data/local-hostname", metadata.token)
	address, addressError := metadata.readValue("meta-data/mesh-ipv6", metadata.token)
	if hostnameError != nil || addressError != nil || (hostname == "" && address == "") {
		metadata.token = ""
		return
	}

	if value, err := metadata.readValue("meta-data/host-time-unix-ms", metadata.token); err == nil && value != "" {
		applyHostTime(value)
	}
	if hostname != "" {
		applyHostname(hostname)
	}
	// A failed read keeps the current routes, because an empty value would remove them.
	if routes, err := metadata.readValue("meta-data/host-routes", metadata.token); err == nil && address != "" {
		if err := metadata.applyMesh(address, strings.FieldsFunc(routes, func(r rune) bool { return r == ',' })); err != nil {
			fmt.Println("guest-init: mesh network:", err)
		}
	}
}

// applyHostTime steps the clock forward after a memory restore. It never steps back.
func applyHostTime(value string) {
	hostMilliseconds, err := strconv.ParseInt(value, 10, 64)
	if err != nil {
		return
	}
	hostTime := time.UnixMilli(hostMilliseconds)
	if time.Until(hostTime) > clockStepThreshold {
		timeValue := syscall.NsecToTimeval(hostTime.UnixNano())
		if err := syscall.Settimeofday(&timeValue); err != nil {
			fmt.Println("guest-init: step clock:", err)
		}
	}
}

func applyHostname(hostname string) {
	if current, err := os.Hostname(); err == nil && current == hostname {
		return
	}
	if err := syscall.Sethostname([]byte(hostname)); err != nil {
		fmt.Println("guest-init: hostname:", err)
		return
	}
	_ = os.WriteFile("/etc/hostname", []byte(hostname+"\n"), 0o644)
	_ = os.WriteFile("/etc/hosts", []byte("127.0.0.1 localhost "+hostname+"\n::1 localhost ip6-localhost ip6-loopback\n"), 0o644)
}
