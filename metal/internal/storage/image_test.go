package storage

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"runtime"
	"slices"
	"strings"
	"testing"

	"github.com/frappe/atlas/metal/internal/vm"
)

func TestManifestIncludesInitrdWithoutRootFileSystemSize(t *testing.T) {
	store := NewStores(t.Context(), "metal", t.TempDir(), nil).Images
	manifest := imageManifest{
		RootfsSHA256: strings.Repeat("a", 64),
		KernelSHA256: strings.Repeat("b", 64),
		InitrdSHA256: strings.Repeat("c", 64),
		Architecture: runtime.GOARCH,
	}
	if err := store.saveImageManifest("ubuntu", manifest); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(store.manifestFile("ubuntu"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), "rootfs_size_bytes") {
		t.Fatalf("manifest contains rootfs_size_bytes: %s", data)
	}

	loaded, found, err := store.loadImageManifest("ubuntu")
	if err != nil {
		t.Fatal(err)
	}
	if !found || loaded != manifest {
		t.Fatalf("manifest = %+v, found = %v", loaded, found)
	}
}

func TestManifestIdentityIncludesInitrd(t *testing.T) {
	first := imageManifest{
		RootfsSHA256: strings.Repeat("a", 64),
		KernelSHA256: strings.Repeat("b", 64),
		InitrdSHA256: strings.Repeat("c", 64),
		Architecture: runtime.GOARCH,
	}
	second := first
	second.InitrdSHA256 = strings.Repeat("d", 64)
	if first.sameImage(second) {
		t.Fatal("initrd digest did not change image identity")
	}
}

func TestManifestRequiresCompleteInitrdMetadata(t *testing.T) {
	image := vm.Image{
		RootfsURL:    "https://images.example/rootfs",
		RootfsSHA256: strings.Repeat("a", 64),
		KernelURL:    "https://images.example/kernel",
		KernelSHA256: strings.Repeat("b", 64),
		Architecture: runtime.GOARCH,
	}
	for _, change := range []func(*vm.Image){
		func(image *vm.Image) { image.InitrdURL = "https://images.example/initrd" },
		func(image *vm.Image) { image.InitrdSHA256 = strings.Repeat("c", 64) },
		func(image *vm.Image) {
			image.InitrdURL = "https://images.example/initrd"
			image.InitrdSHA256 = "short"
		},
	} {
		candidate := image
		change(&candidate)
		if _, err := manifestForImage(candidate); !errors.Is(err, ErrImageIntegrity) {
			t.Fatalf("manifest error = %v, want ErrImageIntegrity", err)
		}
	}
}

func TestEnsureImageArtifactDownloadsAndVerifiesInitrd(t *testing.T) {
	content := []byte("guest initrd")
	digest := fmt.Sprintf("%x", sha256.Sum256(content))
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		_, _ = response.Write(content)
	}))
	defer server.Close()

	store := NewStores(t.Context(), "metal", t.TempDir(), nil).Images
	target := store.initrdFile("ubuntu")
	if err := store.ensureImageArtifact(t.Context(), "ubuntu", target, "initrd", server.URL, digest); err != nil {
		t.Fatal(err)
	}
	stored, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if string(stored) != string(content) {
		t.Fatalf("stored initrd = %q", stored)
	}
}

func TestEnsureImageArtifactRejectsInitrdDigestMismatch(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		_, _ = response.Write([]byte("wrong initrd"))
	}))
	defer server.Close()

	store := NewStores(t.Context(), "metal", t.TempDir(), nil).Images
	target := store.initrdFile("ubuntu")
	err := store.ensureImageArtifact(
		t.Context(), "ubuntu", target, "initrd", server.URL, strings.Repeat("a", 64),
	)
	if !errors.Is(err, ErrImageIntegrity) {
		t.Fatalf("error = %v, want ErrImageIntegrity", err)
	}
	if _, err := os.Stat(target); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("stored initrd exists after digest mismatch: %v", err)
	}
}

func TestEnsureImageRejectsDifferentContentForReference(t *testing.T) {
	imageStore := NewStores(t.Context(), "metal", t.TempDir(), nil).Images
	original := imageManifest{RootfsSHA256: strings.Repeat("a", 64), KernelSHA256: strings.Repeat("b", 64), Architecture: runtime.GOARCH}
	if err := imageStore.saveImageManifest("ubuntu", original); err != nil {
		t.Fatal(err)
	}

	err := imageStore.ensureImage(context.Background(), "ubuntu", vm.Image{
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

func TestEnsureImageRemovesAnIncompleteImport(t *testing.T) {
	logFile := fakeZFS(t, "", "none")
	imageStore := NewStores(t.Context(), "metal", t.TempDir(), nil).Images
	if err := os.MkdirAll(imageStore.imageDirectory("ubuntu"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(imageStore.kernelFile("ubuntu"), []byte("partial"), 0o644); err != nil {
		t.Fatal(err)
	}

	err := imageStore.ensureImage(t.Context(), "ubuntu", vm.Image{
		RootfsURL:    "https://images.example/rootfs",
		RootfsSHA256: strings.Repeat("a", 64),
		KernelURL:    "invalid",
		KernelSHA256: strings.Repeat("b", 64),
		Architecture: runtime.GOARCH,
	})
	if err == nil || errors.Is(err, ErrImageConflict) {
		t.Fatalf("error = %v, want a kernel download failure", err)
	}
	if !slices.Contains(commandLog(t, logFile), "destroy -r metal/images/ubuntu") {
		t.Fatal("incomplete base dataset was not destroyed")
	}
	if _, err := os.Stat(imageStore.kernelFile("ubuntu")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("incomplete kernel remains: %v", err)
	}
}
