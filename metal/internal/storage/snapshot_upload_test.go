package storage

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/klauspost/compress/zstd"
)

// partRecorder stands in for the object store. It records the parts it stored
// and can refuse a chosen part a number of times.
type partRecorder struct {
	mu        sync.Mutex
	bodies    map[int][]byte
	attempts  map[int]int
	failPart  int
	failTimes int
	failCode  int
}

func newPartRecorder() *partRecorder {
	return &partRecorder{bodies: map[int][]byte{}, attempts: map[int]int{}, failCode: http.StatusInternalServerError}
}

func (recorder *partRecorder) server(t *testing.T) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		partNumber := 0
		fmt.Sscanf(request.URL.Query().Get("part"), "%d", &partNumber)
		body := make([]byte, request.ContentLength)
		if _, err := request.Body.Read(body); err != nil && err.Error() != "EOF" {
			t.Errorf("read part %d: %v", partNumber, err)
		}

		recorder.mu.Lock()
		recorder.attempts[partNumber]++
		shouldFail := partNumber == recorder.failPart && recorder.attempts[partNumber] <= recorder.failTimes
		if !shouldFail {
			recorder.bodies[partNumber] = body
		}
		recorder.mu.Unlock()

		if shouldFail {
			writer.WriteHeader(recorder.failCode)
			return
		}
		writer.Header().Set("ETag", fmt.Sprintf("etag-%d", partNumber))
		writer.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(server.Close)
	return server
}

// uploadFixture builds a store with one staged snapshot and one artifact file.
func uploadFixture(t *testing.T, contents []byte) (*SnapshotStore, string, string) {
	t.Helper()
	store := NewStores(t.Context(), "test", filepath.Join(t.TempDir(), "images"), nil).Snapshots
	snapshotID := "snapshot-1"
	now := time.Now().UTC()
	if err := store.saveStagedSnapshot(stagedSnapshotMetadata{
		ID: snapshotID, SourceVirtualMachineID: "vm-1", SourceSnapshot: "test/vms/vm-1@snapshot-1",
		RootfsSizeBytes: int64(len(contents)), KernelSizeBytes: 1, CreatedAt: now, LastActivityAt: now,
	}); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "rootfs.img")
	if err := os.WriteFile(path, contents, 0o600); err != nil {
		t.Fatal(err)
	}
	return store, snapshotID, path
}

func partsFor(server *httptest.Server, count int) []SnapshotUploadPart {
	parts := make([]SnapshotUploadPart, 0, count)
	for number := 1; number <= count; number++ {
		parts = append(parts, SnapshotUploadPart{PartNumber: number, URL: fmt.Sprintf("%s/object?part=%d", server.URL, number)})
	}
	return parts
}

func TestUploadArtifactDigestsTheWholeArtifact(t *testing.T) {
	contents := bytes.Repeat([]byte("atlas"), 1000)
	store, snapshotID, path := uploadFixture(t, contents)
	recorder := newPartRecorder()
	request := SnapshotArtifactUpload{UploadID: "upload-1", Parts: partsFor(recorder.server(t), 1)}

	var uploaded atomic.Int64
	artifact, err := store.uploadArtifact(t.Context(), snapshotID, rootfsArtifact, path, int64(len(contents)), request, &uploaded)
	if err != nil {
		t.Fatal(err)
	}

	sum := sha256.Sum256(contents)
	if artifact.SHA256 != hex.EncodeToString(sum[:]) {
		t.Fatalf("digest = %s", artifact.SHA256)
	}
	if len(artifact.Parts) != 1 || artifact.Parts[0].ETag != "etag-1" {
		t.Fatalf("parts = %+v", artifact.Parts)
	}
	if uploaded.Load() != int64(len(contents)) {
		t.Fatalf("uploaded = %d", uploaded.Load())
	}
}

func TestUploadArtifactRejectsAShortArtifact(t *testing.T) {
	contents := bytes.Repeat([]byte("atlas"), 1000)
	store, snapshotID, path := uploadFixture(t, contents)
	recorder := newPartRecorder()
	request := SnapshotArtifactUpload{UploadID: "upload-1", Parts: partsFor(recorder.server(t), 1)}

	var uploaded atomic.Int64
	_, err := store.uploadArtifact(t.Context(), snapshotID, rootfsArtifact, path, int64(len(contents))+1, request, &uploaded)
	if !errors.Is(err, ErrInvalidUpload) {
		t.Fatalf("error = %v", err)
	}
}

func TestUploadArtifactRepeatsAPartTheStoreRefuses(t *testing.T) {
	contents := bytes.Repeat([]byte("z"), 512)
	store, snapshotID, path := uploadFixture(t, contents)
	recorder := newPartRecorder()
	recorder.failPart, recorder.failTimes = 1, 2
	request := SnapshotArtifactUpload{UploadID: "upload-1", Parts: partsFor(recorder.server(t), 1)}

	var uploaded atomic.Int64
	if _, err := store.uploadArtifact(t.Context(), snapshotID, rootfsArtifact, path, int64(len(contents)), request, &uploaded); err != nil {
		t.Fatalf("a part refused twice must still succeed: %v", err)
	}

	recorder.mu.Lock()
	defer recorder.mu.Unlock()
	if recorder.attempts[1] != 3 {
		t.Fatalf("attempts = %d, want 3", recorder.attempts[1])
	}
	// Every attempt must send the whole part, never a body a failed try drained.
	decoder, err := zstd.NewReader(bytes.NewReader(recorder.bodies[1]))
	if err != nil {
		t.Fatal(err)
	}
	defer decoder.Close()
	decoded, err := io.ReadAll(decoder)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(decoded, contents) {
		t.Fatalf("stored part decodes to %d bytes, want %d", len(decoded), len(contents))
	}
}

func TestUploadArtifactGivesUpOnAFailureItCannotRetry(t *testing.T) {
	contents := bytes.Repeat([]byte("z"), 64)
	store, snapshotID, path := uploadFixture(t, contents)
	recorder := newPartRecorder()
	recorder.failPart, recorder.failTimes, recorder.failCode = 1, 1, http.StatusForbidden
	request := SnapshotArtifactUpload{UploadID: "upload-1", Parts: partsFor(recorder.server(t), 1)}

	var uploaded atomic.Int64
	if _, err := store.uploadArtifact(t.Context(), snapshotID, rootfsArtifact, path, int64(len(contents)), request, &uploaded); err == nil {
		t.Fatal("a rejected part must fail the artifact")
	}
	recorder.mu.Lock()
	defer recorder.mu.Unlock()
	if recorder.attempts[1] != 1 {
		t.Fatalf("attempts = %d, want 1", recorder.attempts[1])
	}
}

func TestUploadArtifactResumesWithoutResendingStoredParts(t *testing.T) {
	contents := bytes.Repeat([]byte("q"), 128)
	store, snapshotID, path := uploadFixture(t, contents)
	recorder := newPartRecorder()
	request := SnapshotArtifactUpload{UploadID: "upload-1", Parts: partsFor(recorder.server(t), 1)}

	var uploaded atomic.Int64
	first, err := store.uploadArtifact(t.Context(), snapshotID, rootfsArtifact, path, int64(len(contents)), request, &uploaded)
	if err != nil {
		t.Fatal(err)
	}

	// The same upload ID resumes. The part is already stored, so nothing is sent.
	second, err := store.uploadArtifact(t.Context(), snapshotID, rootfsArtifact, path, int64(len(contents)), request, &uploaded)
	if err != nil {
		t.Fatal(err)
	}
	recorder.mu.Lock()
	attemptsAfterResume := recorder.attempts[1]
	recorder.mu.Unlock()
	if attemptsAfterResume != 1 {
		t.Fatalf("attempts = %d, want 1: a stored part must not be sent again", attemptsAfterResume)
	}
	if second.SHA256 != first.SHA256 || second.Parts[0].ETag != first.Parts[0].ETag {
		t.Fatalf("resume changed the result: %+v against %+v", second, first)
	}
}

func TestUploadArtifactStartsAgainAfterTheUploadIDChanges(t *testing.T) {
	contents := bytes.Repeat([]byte("q"), 128)
	store, snapshotID, path := uploadFixture(t, contents)
	recorder := newPartRecorder()
	server := recorder.server(t)

	var uploaded atomic.Int64
	if _, err := store.uploadArtifact(t.Context(), snapshotID, rootfsArtifact, path, int64(len(contents)),
		SnapshotArtifactUpload{UploadID: "upload-1", Parts: partsFor(server, 1)}, &uploaded); err != nil {
		t.Fatal(err)
	}
	// The controller replaced the multipart upload, so the stored ETag is worthless.
	if _, err := store.uploadArtifact(t.Context(), snapshotID, rootfsArtifact, path, int64(len(contents)),
		SnapshotArtifactUpload{UploadID: "upload-2", Parts: partsFor(server, 1)}, &uploaded); err != nil {
		t.Fatal(err)
	}

	recorder.mu.Lock()
	defer recorder.mu.Unlock()
	if recorder.attempts[1] != 2 {
		t.Fatalf("attempts = %d, want 2: a replaced upload must send the part again", recorder.attempts[1])
	}
}
