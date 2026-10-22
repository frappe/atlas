package storage

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/klauspost/compress/zstd"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

func TestSleepMemoryUploadPreservesCheckpointAndChecksum(t *testing.T) {
	source := t.TempDir()
	contents := map[string][]byte{"state": []byte("device state"), "memory": make([]byte, 1<<20), "metadata.json": []byte("{}")}
	contents["memory"][1234] = 42
	for name, content := range contents {
		if err := os.WriteFile(filepath.Join(source, name), content, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	object := make(chan []byte, 1)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPut {
			t.Errorf("unexpected method %s", request.Method)
		}
		data, err := io.ReadAll(request.Body)
		if err != nil {
			t.Error(err)
		}
		object <- data
		writer.Header().Set("ETag", "stored-object")
	}))
	defer server.Close()
	store := NewStores(t.Context(), "test", filepath.Join(t.TempDir(), "images"), nil, telemetry.NewRegistry(nil)).Snapshots
	staged := filepath.Join(t.TempDir(), "staged")
	if err := store.StageSleepMemory(t.Context(), source, staged); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(source, "state"), []byte("changed after wake"), 0o600); err != nil {
		t.Fatal(err)
	}
	artifact, err := store.UploadSleepMemory(t.Context(), staged, server.URL+"?put=secret")
	if err != nil {
		t.Fatal(err)
	}
	stored := <-object
	decoder, err := zstd.NewReader(bytes.NewReader(stored))
	if err != nil {
		t.Fatal(err)
	}
	defer decoder.Close()
	raw, err := io.ReadAll(decoder)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(raw)
	if artifact.SHA256 != hex.EncodeToString(digest[:]) || artifact.SizeBytes != int64(len(raw)) || artifact.StoredSizeBytes != int64(len(stored)) {
		t.Fatalf("uploaded checksum or sizes differ: %+v", artifact)
	}
	archive := tar.NewReader(bytes.NewReader(raw))
	for _, name := range []string{"state", "memory", "metadata.json"} {
		header, err := archive.Next()
		if err != nil || header.Name != name {
			t.Fatalf("archive entry differs: %v, %v", header, err)
		}
		actual, err := io.ReadAll(archive)
		if err != nil || !bytes.Equal(actual, contents[name]) {
			t.Fatalf("uploaded %s differs from the staged checkpoint: %v", name, err)
		}
	}
}
