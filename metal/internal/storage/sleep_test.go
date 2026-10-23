package storage

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"math/rand"
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

type sleepUploadTransport func(*http.Request) (*http.Response, error)

func (transport sleepUploadTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	return transport(request)
}

func TestSleepUploadCountsCompressedHTTPAttempts(t *testing.T) {
	contents := bytes.Repeat([]byte("compressible snapshot"), 1<<16)
	store, _, _ := uploadFixture(t, contents)
	progress := &vm.SleepUploadProgress{}
	attempts := 0
	store.httpClient = &http.Client{Transport: sleepUploadTransport(func(request *http.Request) (*http.Response, error) {
		attempts++
		total := progress.TotalBytes.Load()
		if total <= 0 || total >= int64(len(contents)) || request.ContentLength != total || progress.UploadedBytes.Load() != 0 {
			return nil, fmt.Errorf("attempt %d: size=%d, length=%d, read=%d", attempts, total, request.ContentLength, progress.UploadedBytes.Load())
		}
		defer request.Body.Close()
		if _, err := io.CopyN(io.Discard, request.Body, total/2); err != nil {
			return nil, err
		}
		if read := progress.UploadedBytes.Load(); read != total/2 {
			return nil, fmt.Errorf("counted %d compressed bytes, want %d", read, total/2)
		}
		if _, err := io.Copy(io.Discard, request.Body); err != nil {
			return nil, err
		}
		if read := progress.UploadedBytes.Load(); read != total {
			return nil, fmt.Errorf("counted %d compressed bytes, want %d", read, total)
		}
		response := &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(""))}
		response.Header.Set("ETag", "stored")
		if attempts == 1 {
			response.StatusCode = http.StatusServiceUnavailable
		}
		return response, nil
	})}
	artifact, err := store.uploadSleepArtifact(t.Context(), bytes.NewReader(contents), int64(len(contents)), sleepMultipartTarget("https://objects.example/upload"), t.TempDir(), progress)
	if err != nil || attempts != 2 || artifact.StoredSizeBytes != progress.TotalBytes.Load() {
		t.Fatalf("upload after retry: %+v, attempts=%d, error=%v", artifact, attempts, err)
	}
}

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
	artifact, err := store.UploadSleepMemory(t.Context(), staged, sleepMultipartTarget(server.URL+"?put=secret"), &vm.SleepUploadProgress{})
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

func sleepMultipartTarget(destination string) vm.SleepArtifactTarget {
	return vm.SleepArtifactTarget{Multipart: &vm.SleepMultipartUpload{UploadID: "sleep-upload", PartSizeMiB: 5,
		Parts: []vm.SleepUploadPart{{PartNumber: 1, URL: destination}}}}
}

func TestSleepMultipartResumesPartsAndBoundsTemporaryDisk(t *testing.T) {
	contents := make([]byte, 12<<20)
	rand.New(rand.NewSource(7)).Read(contents)
	store, _, _ := uploadFixture(t, contents)
	directory := t.TempDir()
	stored := make(map[int][]byte)
	attempts := make(map[int]int)
	fail := true
	store.httpClient = &http.Client{Transport: sleepUploadTransport(func(request *http.Request) (*http.Response, error) {
		var number int
		fmt.Sscanf(request.URL.Path, "/part/%d", &number)
		attempts[number]++
		buffers, _ := filepath.Glob(filepath.Join(directory, "part-*"))
		if len(buffers) > 2 {
			t.Errorf("buffered %d parts", len(buffers))
		}
		data, err := io.ReadAll(request.Body)
		request.Body.Close()
		if err != nil {
			return nil, err
		}
		response := &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(""))}
		response.Header.Set("ETag", fmt.Sprintf("part-%d", number))
		if number == 2 && fail {
			response.StatusCode = http.StatusBadRequest
		} else {
			stored[number] = data
		}
		return response, nil
	})}
	target := sleepMultipartTarget("https://objects.example/part/1")
	for number := 2; number <= 3; number++ {
		target.Multipart.Parts = append(target.Multipart.Parts, vm.SleepUploadPart{PartNumber: number, URL: fmt.Sprintf("https://objects.example/part/%d", number)})
	}
	if _, err := store.uploadSleepArtifact(t.Context(), bytes.NewReader(contents), int64(len(contents)), target, directory, &vm.SleepUploadProgress{}); err == nil {
		t.Fatal("part failure was hidden")
	}
	fail = false
	artifact, err := store.uploadSleepArtifact(t.Context(), bytes.NewReader(contents), int64(len(contents)), target, directory, &vm.SleepUploadProgress{})
	if err != nil {
		t.Fatal(err)
	}
	if attempts[1] != 1 || attempts[2] != 2 || attempts[3] != 1 || len(artifact.Parts) != 3 {
		t.Fatalf("did not resume uploaded parts: %v, %+v", attempts, artifact)
	}
	var compressed bytes.Buffer
	for number := 1; number <= 3; number++ {
		compressed.Write(stored[number])
	}
	decoder, err := zstd.NewReader(&compressed)
	if err != nil {
		t.Fatal(err)
	}
	defer decoder.Close()
	actual, err := io.ReadAll(decoder)
	if err != nil || !bytes.Equal(actual, contents) {
		t.Fatalf("multipart round trip differs: %v", err)
	}
	if buffers, _ := filepath.Glob(filepath.Join(directory, "part-*")); len(buffers) != 0 {
		t.Fatalf("left buffers: %v", buffers)
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
