package storage

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/frappe/atlas/metal/internal/vm"
)

func TestNames(t *testing.T) {
	pool := NewStores("metal", "/images").Pool
	if got := pool.baseDataset("ubuntu"); got != "metal/images/ubuntu" {
		t.Errorf("baseDataset = %q", got)
	}
	if got := pool.baseSnapshot("ubuntu"); got != "metal/images/ubuntu@ready" {
		t.Errorf("baseSnapshot = %q", got)
	}
	if got := pool.virtualMachineDataset("abc"); got != "metal/vms/abc" {
		t.Errorf("virtualMachineDataset = %q", got)
	}
	if got := pool.virtualMachineDevicePath("abc"); got != "/dev/zvol/metal/vms/abc" {
		t.Errorf("virtualMachineDevicePath = %q", got)
	}
}

func TestLinkOrCopyUsesHardLinkOnOneFileSystem(t *testing.T) {
	directory := t.TempDir()
	source := filepath.Join(directory, "source")
	destination := filepath.Join(directory, "destination")
	if err := os.WriteFile(source, []byte("guest-memory"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := LinkOrCopy(context.Background(), source, destination); err != nil {
		t.Fatal(err)
	}

	content, err := os.ReadFile(destination)
	if err != nil || string(content) != "guest-memory" {
		t.Fatalf("destination = %q, error %v", content, err)
	}
	sourceInformation, err := os.Stat(source)
	if err != nil {
		t.Fatal(err)
	}
	destinationInformation, err := os.Stat(destination)
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(sourceInformation, destinationInformation) {
		t.Error("local files do not share one inode")
	}
}

func TestKernelArguments(t *testing.T) {
	directory := t.TempDir()
	if got := kernelArguments(directory); got != defaultKernelArguments {
		t.Errorf("default = %q", got)
	}
	if err := os.WriteFile(filepath.Join(directory, "boot-args"), []byte("custom args\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := kernelArguments(directory); got != "custom args" {
		t.Errorf("override = %q", got)
	}
}

func TestEnsureImageRejectsDifferentContentForReference(t *testing.T) {
	imageStore := NewStores("metal", t.TempDir()).Images
	original := imageManifest{RootfsSHA256: strings.Repeat("a", 64), KernelSHA256: strings.Repeat("b", 64), Architecture: runtime.GOARCH}
	if err := imageStore.saveImageManifest("ubuntu", original); err != nil {
		t.Fatal(err)
	}

	err := imageStore.ensureImage(context.Background(), "ubuntu", vm.ImageRef{
		RootfsURL:    "https://images.example/rootfs?signature=secret",
		RootfsSHA256: strings.Repeat("c", 64),
		KernelURL:    "https://images.example/kernel?signature=secret",
		KernelSHA256: original.KernelSHA256,
		Architecture: runtime.GOARCH,
	})
	if !errors.Is(err, ErrImageConflict) {
		t.Fatalf("error = %v, want ErrImageConflict", err)
	}
	if strings.Contains(err.Error(), "secret") {
		t.Fatal("error contains a signed URL query value")
	}
}

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
