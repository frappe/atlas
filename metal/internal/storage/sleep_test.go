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
	"strings"
	"testing"

	"github.com/klauspost/compress/zstd"

	"github.com/frappe/atlas/metal/internal/telemetry"
	"github.com/frappe/atlas/metal/internal/vm"
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
	downloadServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodGet {
			t.Errorf("unexpected method %s", request.Method)
		}
		writer.Write(stored)
	}))
	defer downloadServer.Close()
	restored := t.TempDir()
	if err := store.DownloadSleepMemory(t.Context(), restored, downloadServer.URL+"?get=secret", artifact); err != nil {
		t.Fatal(err)
	}
	for name, content := range contents {
		actual, err := os.ReadFile(filepath.Join(restored, name))
		if err != nil || !bytes.Equal(actual, content) {
			t.Fatalf("restored %s differs: %v", name, err)
		}
	}
	artifact.SHA256 = strings.Repeat("0", 64)
	corrupt := t.TempDir()
	if err := store.DownloadSleepMemory(t.Context(), corrupt, downloadServer.URL, artifact); err == nil {
		t.Fatal("corrupt checkpoint accepted")
	}
	entries, err := os.ReadDir(corrupt)
	if err != nil || len(entries) != 0 {
		t.Fatalf("failed download left files: %v, %v", entries, err)
	}
}

func TestSleepMemoryArchiveRejectsPathsAndLinks(t *testing.T) {
	for _, header := range []*tar.Header{
		{Name: "../outside", Mode: 0o600, Size: 1},
		{Name: "state", Typeflag: tar.TypeSymlink, Linkname: "../outside"},
	} {
		var data bytes.Buffer
		writer := tar.NewWriter(&data)
		if err := writer.WriteHeader(header); err != nil {
			t.Fatal(err)
		}
		if header.Size > 0 {
			writer.Write([]byte("x"))
		}
		if err := writer.Close(); err != nil {
			t.Fatal(err)
		}
		if err := extractSleepMemory(t.Context(), &data, t.TempDir()); err == nil {
			t.Fatalf("unsafe archive entry accepted: %+v", header)
		}
	}
}

func TestSleepDiskRestoreDoesNotReplaceExistingDisk(t *testing.T) {
	logFile := fakeZFS(t, "", "none")
	store := &SnapshotStore{pool: &ZFSPool{name: "metal"}}
	if err := store.RestoreSleepDisk(t.Context(), "vm-1", "https://unused.example", vm.SleepArtifact{}); err != nil {
		t.Fatal(err)
	}
	if commands := commandLog(t, logFile); len(commands) != 1 || commands[0] != "list metal/vms/vm-1" {
		t.Fatalf("existing disk was changed: %v", commands)
	}
}
