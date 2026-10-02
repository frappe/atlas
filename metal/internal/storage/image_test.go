package storage

import (
	"context"
	"errors"
	"os"
	"runtime"
	"slices"
	"strings"
	"testing"

	"github.com/frappe/atlas/metal/internal/vm"
)

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
