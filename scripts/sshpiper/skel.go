package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"net"
	"os"
	"strings"
	"time"

	"github.com/tg123/sshpiper/libplugin"
	"github.com/tg123/sshpiper/libplugin/skel"
)

type atlasFactory struct {
	atlasURL       string
	lookupServer   string
	apiKey         string
	privateKeyPath string
	targetUser     string
}

type atlasLookupResponse struct {
	Message atlasLookupMessage `json:"message"`
}

type atlasLookupMessage struct {
	VirtualMachine string   `json:"virtual_machine"`
	Title          string   `json:"title"`
	Server         string   `json:"server"`
	IPv6Address    string   `json:"ipv6_address"`
	Host           string   `json:"host"`
	PublicKeys     []string `json:"public_keys"`
}

type atlasPipe struct {
	factory *atlasFactory
	lookup  atlasLookupMessage
}

type atlasPipeFrom struct {
	pipe *atlasPipe
}

type atlasPipeToPrivateKey struct {
	pipe *atlasPipe
}

func (p *atlasPipe) From() []skel.SkelPipeFrom {
	return []skel.SkelPipeFrom{&atlasPipeFrom{pipe: p}}
}

func (p *atlasPipeToPrivateKey) User(conn libplugin.ConnMetadata) string {
	return p.pipe.factory.targetUser
}

func (p *atlasPipeToPrivateKey) Host(conn libplugin.ConnMetadata) string {
	host := p.pipe.lookup.IPv6Address
	return net.JoinHostPort(host, "22")
}

func (p *atlasPipeToPrivateKey) KnownHosts(conn libplugin.ConnMetadata) ([]byte, error) {
	return nil, nil
}

func (p *atlasPipeToPrivateKey) IgnoreHostKey(conn libplugin.ConnMetadata) bool {
	return true
}

func (p *atlasPipeFrom) MatchConn(conn libplugin.ConnMetadata) (skel.SkelPipeTo, error) {
	return &atlasPipeToPrivateKey{pipe: p.pipe}, nil
}

func (p *atlasPipeFrom) AuthorizedKeys(conn libplugin.ConnMetadata) ([]byte, error) {
	var buffer bytes.Buffer
	for _, key := range p.pipe.lookup.PublicKeys {
		key = strings.TrimSpace(key)
		if key == "" {
			continue
		}
		buffer.WriteString(key)
		buffer.WriteByte('\n')
	}
	return buffer.Bytes(), nil
}

func (p *atlasPipeFrom) TrustedUserCAKeys(conn libplugin.ConnMetadata) ([]byte, error) {
	return nil, nil
}

func (p *atlasPipeToPrivateKey) PrivateKey(conn libplugin.ConnMetadata) ([]byte, []byte, error) {
	key, err := os.ReadFile(p.pipe.factory.privateKeyPath)
	if err != nil {
		return nil, nil, err
	}
	return key, nil, nil
}

func (f *atlasFactory) listPipe(conn libplugin.ConnMetadata) ([]skel.SkelPipe, error) {
	if err := f.validate(); err != nil {
		return nil, err
	}

	lookup, err := f.lookup(conn.User())
	if err != nil {
		return nil, err
	}
	if len(lookup.PublicKeys) == 0 {
		return nil, fmt.Errorf("atlas lookup for %q returned no public keys", conn.User())
	}
	if lookup.Host == "" && lookup.IPv6Address == "" {
		return nil, fmt.Errorf("atlas lookup for %q returned no host", conn.User())
	}

	return []skel.SkelPipe{&atlasPipe{factory: f, lookup: lookup}}, nil
}

func (f *atlasFactory) validate() error {
	if strings.TrimSpace(f.atlasURL) == "" {
		return fmt.Errorf("ATLAS_URL is required")
	}
	if strings.TrimSpace(f.lookupServer) == "" {
		return fmt.Errorf("SSHPIPER_LOOKUP_SERVER is required")
	}
	if strings.TrimSpace(f.apiKey) == "" {
		return fmt.Errorf("SSHPIPER_API_KEY is required")
	}
	if strings.TrimSpace(f.privateKeyPath) == "" {
		return fmt.Errorf("SSHPIPER_PRIVATE_KEY is required")
	}
	if strings.TrimSpace(f.targetUser) == "" {
		return fmt.Errorf("SSHPIPER_TARGET_USER is required")
	}
	return nil
}

func (f *atlasFactory) lookup(vmName string) (atlasLookupMessage, error) {
	baseURL := strings.TrimRight(f.atlasURL, "/")
	endpoint := baseURL + "/api/method/atlas.atlas.doctype.server.server.lookup_virtual_machine_ssh"
	values := url.Values{}
	values.Set("server", f.lookupServer)
	values.Set("vm_name", vmName)

	request, err := http.NewRequest(http.MethodGet, endpoint+"?"+values.Encode(), nil)
	if err != nil {
		return atlasLookupMessage{}, err
	}
	request.Header.Set("X-Atlas-Server-Token", f.apiKey)

	client := &http.Client{Timeout: 10 * time.Second}
	response, err := client.Do(request)
	if err != nil {
		return atlasLookupMessage{}, err
	}
	defer response.Body.Close()

	body, err := io.ReadAll(response.Body)
	if err != nil {
		return atlasLookupMessage{}, err
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return atlasLookupMessage{}, fmt.Errorf("atlas lookup failed: status=%d body=%s", response.StatusCode, strings.TrimSpace(string(body)))
	}

	var payload atlasLookupResponse
	if err := json.Unmarshal(body, &payload); err != nil {
		return atlasLookupMessage{}, err
	}
	return payload.Message, nil
}
