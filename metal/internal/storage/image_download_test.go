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
	"path/filepath"
	"strings"
	"testing"

	"github.com/klauspost/compress/zstd"
)

func TestDownloadVerifiesDigestAndRedactsURL(t *testing.T) {
	content := []byte("verified image")
	digest := sha256.Sum256(content)
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		_, _ = response.Write(content)
	}))
	defer server.Close()

	destination := &temporaryFileDestination{directory: t.TempDir()}
	if err := downloadOnce(context.Background(), server.Client(), server.URL+"?signature=secret", hex.EncodeToString(digest[:]), destination); err != nil {
		t.Fatal(err)
	}

	destination = &temporaryFileDestination{directory: t.TempDir()}
	if err := downloadOnce(context.Background(), server.Client(), server.URL+"?signature=secret", strings.Repeat("0", 64), destination); !errors.Is(err, ErrImageIntegrity) {
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

	err := download(context.Background(), server.Client(), server.URL, strings.Repeat("0", 64), &temporaryFileDestination{directory: t.TempDir()})
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

// countingDestination counts the writes a download makes.
type countingDestination struct {
	writes  int
	content bytes.Buffer
}

func (destination *countingDestination) open(context.Context, int64) (io.Writer, error) {
	return destination, nil
}

func (destination *countingDestination) Write(data []byte) (int, error) {
	destination.writes++
	return destination.content.Write(data)
}

func (destination *countingDestination) commit() error { return nil }
func (destination *countingDestination) discard()      {}

func serveArtifact(t *testing.T, body []byte) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		_, _ = response.Write(body)
	}))
	t.Cleanup(server.Close)
	return server
}

func TestDownloadWritesInFullBuffers(t *testing.T) {
	content := bytes.Repeat([]byte{1}, 4<<20)
	digest := sha256.Sum256(content)
	server := serveArtifact(t, content)

	destination := &countingDestination{}
	if err := downloadOnce(t.Context(), server.Client(), server.URL, hex.EncodeToString(digest[:]), destination); err != nil {
		t.Fatal(err)
	}
	if destination.writes != 4 {
		t.Fatalf("writes = %d for 4 MiB, want 4", destination.writes)
	}
}

func TestDownloadDecodesAZstdArtifactWithItsContentSize(t *testing.T) {
	content := bytes.Repeat([]byte("atlas"), 1000)
	digest := sha256.Sum256(content)
	encoder, err := zstd.NewWriter(nil)
	if err != nil {
		t.Fatal(err)
	}
	server := serveArtifact(t, encoder.EncodeAll(content, nil))

	destination := &countingDestination{}
	if err := downloadOnce(t.Context(), server.Client(), server.URL, hex.EncodeToString(digest[:]), destination); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(destination.content.Bytes(), content) {
		t.Fatal("decoded content does not match")
	}
}

func TestSparseVolumeWriterSkipsZeroChunks(t *testing.T) {
	file, err := os.Create(filepath.Join(t.TempDir(), "volume"))
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	if _, err := file.Write(bytes.Repeat([]byte{0xff}, 8)); err != nil {
		t.Fatal(err)
	}

	writer := &sparseVolumeWriter{file: file}
	if _, err := writer.Write(make([]byte, 4)); err != nil {
		t.Fatal(err)
	}
	if _, err := writer.Write([]byte("data")); err != nil {
		t.Fatal(err)
	}

	stored, err := os.ReadFile(file.Name())
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(stored, append(bytes.Repeat([]byte{0xff}, 4), "data"...)) {
		t.Fatalf("stored = %v", stored)
	}
}
