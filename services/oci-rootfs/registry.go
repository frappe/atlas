package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"runtime"
	"strings"
)

const (
	dockerHubRegistry = "registry-1.docker.io"

	// manifestMediaTypes names every manifest form that a registry can return.
	manifestMediaTypes = "application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, " +
		"application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json"
)

var (
	referencePattern   = regexp.MustCompile(`^(?:([^/]*[.:][^/]*)/)?([^:@]+)(?::([^@]+))?(?:@(sha256:[0-9a-f]{64}))?$`)
	bearerFieldPattern = regexp.MustCompile(`(realm|service|scope)="([^"]*)"`)
)

type descriptor struct {
	MediaType string `json:"mediaType"`
	Digest    string `json:"digest"`
	Platform  struct {
		Architecture string `json:"architecture"`
		OS           string `json:"os"`
	} `json:"platform"`
}

type manifest struct {
	Manifests []descriptor `json:"manifests"`
	Config    descriptor   `json:"config"`
	Layers    []descriptor `json:"layers"`
}

// registryClient pulls from one repository. Every digest is verified, so a pull
// by digest does not need to trust the registry.
type registryClient struct {
	baseURL    string
	repository string
	token      string
}

// parseReference returns the client and the manifest tag or digest of a
// reference, such as nginx:1.29-alpine or ghcr.io/org/app@sha256:...
func parseReference(reference string) (*registryClient, string, error) {
	matches := referencePattern.FindStringSubmatch(reference)
	if matches == nil {
		return nil, "", fmt.Errorf("invalid image reference %q", reference)
	}
	registry, repository := matches[1], matches[2]
	if registry == "" || registry == "docker.io" {
		registry = dockerHubRegistry
	}
	if registry == dockerHubRegistry && !strings.Contains(repository, "/") {
		repository = "library/" + repository
	}

	manifestReference := matches[4]
	if manifestReference == "" {
		manifestReference = matches[3]
	}
	if manifestReference == "" {
		manifestReference = "latest"
	}
	return &registryClient{baseURL: "https://" + registry, repository: repository}, manifestReference, nil
}

// fetchImageManifest returns the manifest for the architecture of this binary.
func (registry *registryClient) fetchImageManifest(ctx context.Context, reference string) (manifest, error) {
	result, err := registry.fetchManifest(ctx, reference)
	if err != nil || len(result.Manifests) == 0 {
		return result, err
	}
	for _, candidate := range result.Manifests {
		if candidate.Platform.OS == "linux" && candidate.Platform.Architecture == runtime.GOARCH {
			return registry.fetchManifest(ctx, candidate.Digest)
		}
	}
	return manifest{}, fmt.Errorf("the image has no linux/%s manifest", runtime.GOARCH)
}

func (registry *registryClient) fetchManifest(ctx context.Context, reference string) (manifest, error) {
	var result manifest
	data, err := registry.read(ctx, "manifests/"+reference, reference)
	if err != nil {
		return result, err
	}
	return result, json.Unmarshal(data, &result)
}

// read returns one registry object. It verifies the digest of a reference that has one.
func (registry *registryClient) read(ctx context.Context, resource, reference string) ([]byte, error) {
	body, err := registry.get(ctx, resource)
	if err != nil {
		return nil, err
	}
	defer body.Close()
	data, err := io.ReadAll(body)
	if err != nil {
		return nil, err
	}
	if strings.HasPrefix(reference, "sha256:") {
		sum := sha256.Sum256(data)
		if "sha256:"+hex.EncodeToString(sum[:]) != reference {
			return nil, fmt.Errorf("digest mismatch for %s", resource)
		}
	}
	return data, nil
}

// get opens one registry path. A registry names its token service in a 401 answer.
func (registry *registryClient) get(ctx context.Context, resource string) (io.ReadCloser, error) {
	address := registry.baseURL + "/v2/" + registry.repository + "/" + resource
	response, err := registry.send(ctx, address)
	if err != nil {
		return nil, err
	}
	// A token can expire during a long pull, so every 401 asks for a new one.
	if response.StatusCode == http.StatusUnauthorized {
		challenge := response.Header.Get("WWW-Authenticate")
		response.Body.Close()
		if err := registry.authenticate(ctx, challenge); err != nil {
			return nil, err
		}
		if response, err = registry.send(ctx, address); err != nil {
			return nil, err
		}
	}
	if response.StatusCode != http.StatusOK {
		response.Body.Close()
		return nil, fmt.Errorf("registry %s: status %d", resource, response.StatusCode)
	}
	return response.Body, nil
}

func (registry *registryClient) send(ctx context.Context, address string) (*http.Response, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, address, nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Accept", manifestMediaTypes)
	if registry.token != "" {
		request.Header.Set("Authorization", "Bearer "+registry.token)
	}
	return http.DefaultClient.Do(request)
}

// authenticate gets an anonymous pull token from the service in the challenge.
func (registry *registryClient) authenticate(ctx context.Context, challenge string) error {
	fields := map[string]string{}
	for _, match := range bearerFieldPattern.FindAllStringSubmatch(challenge, -1) {
		fields[match[1]] = match[2]
	}
	address, err := url.Parse(fields["realm"])
	if err != nil || address.Scheme != "https" {
		return fmt.Errorf("registry token service %q is not HTTPS", fields["realm"])
	}
	query := address.Query()
	query.Set("service", fields["service"])
	query.Set("scope", fields["scope"])
	address.RawQuery = query.Encode()

	request, err := http.NewRequestWithContext(ctx, http.MethodGet, address.String(), nil)
	if err != nil {
		return err
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	var token struct {
		Token       string `json:"token"`
		AccessToken string `json:"access_token"`
	}
	if err := json.NewDecoder(response.Body).Decode(&token); err != nil {
		return fmt.Errorf("decode registry token: %w", err)
	}
	registry.token = token.Token
	if registry.token == "" {
		registry.token = token.AccessToken
	}
	if registry.token == "" {
		return errors.New("registry returned no token")
	}
	return nil
}
