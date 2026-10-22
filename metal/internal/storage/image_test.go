package storage

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"runtime"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/vm"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

func TestEnsureImageRejectsDifferentContentForReference(t *testing.T) {
	imageStore := NewStores(t.Context(), "metal", t.TempDir(), nil, telemetry.NewRegistry(nil)).Images
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
	imageStore := NewStores(t.Context(), "metal", t.TempDir(), nil, telemetry.NewRegistry(nil)).Images
	if err := os.MkdirAll(imageStore.imageDirectory("ubuntu"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(imageStore.manifestFile("ubuntu")+".tmp", []byte("partial"), 0o644); err != nil {
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
	if _, err := os.Stat(imageStore.imageDirectory("ubuntu")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("incomplete image directory remains: %v", err)
	}
}

func TestPruneKernelsKeepsKernelsInUse(t *testing.T) {
	imageStore := NewStores(t.Context(), "metal", t.TempDir(), nil, telemetry.NewRegistry(nil)).Images
	imageKernel, virtualMachineKernel, recentKernel, idleKernel := strings.Repeat("a", 64), strings.Repeat("b", 64), strings.Repeat("c", 64), strings.Repeat("d", 64)
	manifest := imageManifest{RootfsSHA256: strings.Repeat("e", 64), KernelSHA256: imageKernel, Architecture: runtime.GOARCH}
	if err := imageStore.saveImageManifest("ubuntu", manifest); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(imageStore.kernelsDirectory, 0o755); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	for _, kernel := range []string{imageKernel, virtualMachineKernel, recentKernel, idleKernel} {
		if err := os.WriteFile(imageStore.kernelFile(kernel), []byte(kernel), 0o644); err != nil {
			t.Fatal(err)
		}
		if kernel != recentKernel {
			if err := os.Chtimes(imageStore.kernelFile(kernel), now, now.Add(-2*time.Hour)); err != nil {
				t.Fatal(err)
			}
		}
	}

	if err := imageStore.PruneKernels([]string{strings.ToUpper(virtualMachineKernel)}, now, time.Hour); err != nil {
		t.Fatal(err)
	}

	for _, kernel := range []string{imageKernel, virtualMachineKernel, recentKernel} {
		if _, err := os.Stat(imageStore.kernelFile(kernel)); err != nil {
			t.Fatalf("kernel %s was removed: %v", kernel[:1], err)
		}
	}
	if _, err := os.Stat(imageStore.kernelFile(idleKernel)); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("idle kernel remains: %v", err)
	}
}

func TestStoreKernelKeepsOnlyAMatchingKernel(t *testing.T) {
	imageStore := NewStores(t.Context(), "metal", t.TempDir(), nil, telemetry.NewRegistry(nil)).Images
	kernel := []byte("vmlinux")
	digest := sha256.Sum256(kernel)
	kernelSHA256 := hex.EncodeToString(digest[:])

	if err := imageStore.StoreKernel(kernelSHA256, bytes.NewReader([]byte("other"))); !errors.Is(err, ErrImageIntegrity) {
		t.Fatalf("mismatched kernel error = %v", err)
	}
	if hasKernel, err := imageStore.HasKernel(kernelSHA256); err != nil || hasKernel {
		t.Fatalf("mismatched kernel stored: %v %v", hasKernel, err)
	}

	if err := imageStore.StoreKernel(kernelSHA256, bytes.NewReader(kernel)); err != nil {
		t.Fatal(err)
	}
	stored, err := os.ReadFile(imageStore.kernelFile(kernelSHA256))
	if err != nil || !bytes.Equal(stored, kernel) {
		t.Fatalf("stored kernel = %q, %v", stored, err)
	}
}
