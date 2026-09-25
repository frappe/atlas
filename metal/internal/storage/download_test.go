package storage

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func TestDownloadVerifiesDigestAndRedactsURL(t *testing.T) {
	content := []byte("verified image")
	digest := sha256.Sum256(content)
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		_, _ = response.Write(content)
	}))
	defer server.Close()

	path, err := downloadOnce(context.Background(), server.Client(), t.TempDir(), server.URL+"?signature=secret", hex.EncodeToString(digest[:]))
	if err != nil {
		t.Fatal(err)
	}
	defer os.Remove(path)

	if _, err := downloadOnce(context.Background(), server.Client(), t.TempDir(), server.URL+"?signature=secret", strings.Repeat("0", 64)); !errors.Is(err, ErrImageIntegrity) {
		t.Fatalf("error = %v, want ErrImageIntegrity", err)
	}
	if got := redactURL(server.URL + "?signature=secret"); strings.Contains(got, "secret") {
		t.Fatalf("redacted URL = %q", got)
	}
}

func TestDownloadDoesNotRetryPermanentFailure(t *testing.T) {
	requestCount := 0
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		requestCount++
		response.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()

	_, err := download(context.Background(), server.Client(), t.TempDir(), server.URL, strings.Repeat("0", 64))
	if err == nil {
		t.Fatal("download succeeded")
	}
	if requestCount != 1 {
		t.Fatalf("request count = %d, want 1", requestCount)
	}
}

func TestImageHTTPClientHasTimeout(t *testing.T) {
	if timeout := newImageHTTPClient().Timeout; timeout <= 0 {
		t.Fatalf("timeout = %s, want a positive duration", timeout)
	}
}

type writeCounter struct{ writes int }

func (counter *writeCounter) Write(data []byte) (int, error) {
	counter.writes++
	return len(data), nil
}

func TestUncompressedArtifactIsCopiedInLargeWrites(t *testing.T) {
	content := bytes.Repeat([]byte{1}, 4<<20)
	// The HTTP body reader has no WriteTo, so hide the one bytes.Reader has.
	body, closeBody, err := decompressedBody(struct{ io.Reader }{bytes.NewReader(content)})
	if err != nil {
		t.Fatal(err)
	}
	defer closeBody()

	counter := &writeCounter{}
	if _, err := io.Copy(io.MultiWriter(counter, sha256.New()), body); err != nil {
		t.Fatal(err)
	}
	if counter.writes > 8 {
		t.Fatalf("writes = %d for 4 MiB, want large writes", counter.writes)
	}
}
