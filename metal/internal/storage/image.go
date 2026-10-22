package storage

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"time"

	"github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/telemetry"
	"github.com/frappe/atlas/metal/internal/vm"
)

const (
	// imageVolumeBlockSize matches the guest file system block size, so a write
	// inside the guest does not turn into a read and rewrite on the host.
	imageVolumeBlockSize = "16k"

	// lastUsedFileName records when a VM last started from an image.
	lastUsedFileName = "last-used"
)

// deleteImage removes one image and its warm artifacts. An image with a VM disk
// cloned from it reports ErrInUse instead of being destroyed.
func (store *ImageStore) deleteImage(ctx context.Context, imageReference string) error {
	lock := store.imageLock(imageReference)
	lock.Lock()
	defer lock.Unlock()

	if err := store.RemoveWarmImages(ctx, imageReference); err != nil {
		return err
	}
	err := platform.Run(ctx, "zfs", "destroy", "-r", store.pool.baseDataset(imageReference))
	switch {
	case err == nil, strings.Contains(err.Error(), "does not exist"):
	case strings.Contains(err.Error(), "dependent clone"):
		return ErrInUse
	default:
		return err
	}

	if err := os.RemoveAll(store.imageDirectory(imageReference)); err != nil {
		return err
	}
	return nil
}

// imageManifest records the content an image reference is bound to.
type imageManifest struct {
	RootfsSHA256 string `json:"rootfs_sha256,omitempty"`
	KernelSHA256 string `json:"kernel_sha256,omitempty"`
	Architecture string `json:"architecture"`
}

// ensureImage makes one image present and verified. A reference is bound to its
// content: the same name with different digests is a conflict, never an update.
// A partial import is removed, so a later call starts clean rather than resuming.
func (store *ImageStore) ensureImage(ctx context.Context, imageReference string, image vm.Image) error {
	lock := store.imageLock(imageReference)
	lock.Lock()
	defer lock.Unlock()

	storedManifest, found, err := store.loadImageManifest(imageReference)
	if err != nil {
		return err
	}
	manifest, err := manifestForImage(image)
	if err != nil {
		return err
	}
	if manifest.Architecture != runtime.GOARCH {
		return fmt.Errorf("%w: image architecture does not match the host", ErrImageIntegrity)
	}
	if found && storedManifest != manifest {
		return fmt.Errorf("%w: image reference %q already identifies different content", ErrImageConflict, imageReference)
	}
	artifactsExist, err := datasetExists(ctx, store.pool.baseDataset(imageReference))
	if err != nil {
		return err
	}
	// The manifest is written last and disks clone only after it exists, so
	// artifacts without one are an unfinished import that is safe to remove.
	if !found && artifactsExist {
		if err := store.removeImageArtifacts(ctx, imageReference); err != nil {
			return fmt.Errorf("remove incomplete image %q: %w", imageReference, err)
		}
	}

	// An import that does not reach the manifest write leaves nothing behind.
	completed := found
	if !found {
		startedAt := time.Now()
		defer func() {
			store.recordImport(imageReference, startedAt, completed)
			if completed {
				return
			}
			if err := store.removeImageArtifacts(context.Background(), imageReference); err != nil {
				store.logger.Warn("remove incomplete image", "image", imageReference, "error", err)
			}
		}()
	}

	if err := store.ensureKernel(ctx, image.KernelURL, manifest.KernelSHA256); err != nil {
		return err
	}
	exists, err := datasetExists(ctx, store.pool.baseDataset(imageReference))
	if err != nil {
		return err
	}
	if !exists {
		if err := store.importRootFileSystem(ctx, imageReference, image.RootfsURL, manifest.RootfsSHA256); err != nil {
			return err
		}
	}
	if !found {
		if err := store.saveImageManifest(imageReference, manifest); err != nil {
			return fmt.Errorf("save image manifest: %w", err)
		}
		completed = true
	}
	return nil
}

// recordImport logs and observes one image import, which a VM create can wait for.
func (store *ImageStore) recordImport(imageReference string, startedAt time.Time, completed bool) {
	result := "ok"
	if !completed {
		result = "failed"
	}
	duration := time.Since(startedAt)
	store.telemetry.Count("metal_image_imports_total", 1, telemetry.Labels{"result": result})
	store.telemetry.Observe("metal_image_import_duration_seconds", duration.Seconds(), telemetry.Labels{"result": result})
	store.logger.Info("image import finished", "component", "storage", "image", imageReference, "result", result, "duration", duration)
}

// removeImageArtifacts deletes the base dataset and directory of one image.
func (store *ImageStore) removeImageArtifacts(ctx context.Context, imageReference string) error {
	if err := destroyIfPresent(ctx, store.pool.baseDataset(imageReference)); err != nil {
		return err
	}
	return os.RemoveAll(store.imageDirectory(imageReference))
}

// manifestForImage builds and validates the manifest a policy asks for.
func manifestForImage(image vm.Image) (imageManifest, error) {
	if image.RootfsURL == "" || image.KernelURL == "" {
		return imageManifest{}, fmt.Errorf("%w: image and kernel URLs are required", ErrImageIntegrity)
	}
	manifest := imageManifest{
		RootfsSHA256: strings.ToLower(image.RootfsSHA256),
		KernelSHA256: strings.ToLower(image.KernelSHA256),
		Architecture: image.Architecture,
	}
	if !validSHA256(manifest.RootfsSHA256) || !validSHA256(manifest.KernelSHA256) {
		return imageManifest{}, fmt.Errorf("%w: rootfs and kernel SHA-256 digests are required", ErrImageIntegrity)
	}
	if manifest.Architecture == "" {
		return imageManifest{}, fmt.Errorf("%w: image architecture is required", ErrImageIntegrity)
	}
	return manifest, nil
}

// validSHA256 reports whether value is a full hexadecimal SHA-256 digest.
func validSHA256(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

// loadImageManifest reads the stored manifest and reports whether one exists.
func (store *ImageStore) loadImageManifest(imageReference string) (imageManifest, bool, error) {
	data, err := os.ReadFile(store.manifestFile(imageReference))
	if errors.Is(err, os.ErrNotExist) {
		return imageManifest{}, false, nil
	}
	if err != nil {
		return imageManifest{}, false, fmt.Errorf("read image manifest: %w", err)
	}

	var manifest imageManifest
	if err := json.Unmarshal(data, &manifest); err != nil {
		return imageManifest{}, false, fmt.Errorf("decode image manifest: %w", err)
	}
	if manifest.Architecture == "" {
		return imageManifest{}, false, fmt.Errorf("%w: stored image manifest is invalid", ErrImageIntegrity)
	}
	if !validSHA256(manifest.RootfsSHA256) || !validSHA256(manifest.KernelSHA256) {
		return imageManifest{}, false, fmt.Errorf("%w: stored image manifest is invalid", ErrImageIntegrity)
	}
	return manifest, true, nil
}

// saveImageManifest publishes the manifest that marks an import complete.
func (store *ImageStore) saveImageManifest(imageReference string, manifest imageManifest) error {
	data, err := json.Marshal(manifest)
	if err != nil {
		return err
	}
	data = append(data, '\n')

	return platform.WriteFile(store.manifestFile(imageReference), data, 0o644)
}

// ensureKernel makes one kernel present and verified. A stored kernel that
// fails verification is an integrity error, not a reason to download again.
func (store *ImageStore) ensureKernel(ctx context.Context, kernelURL, expectedDigest string) error {
	target := store.kernelFile(expectedDigest)
	if _, err := os.Stat(target); err == nil {
		if err := verifyFileSHA256(target, expectedDigest); err != nil {
			return fmt.Errorf("%w: stored kernel verification failed", ErrImageIntegrity)
		}
		return os.Chmod(target, 0o644)
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}

	kernel := &temporaryFileDestination{directory: store.directory}
	if err := download(ctx, store.httpClient, kernelURL, expectedDigest, kernel, store.logger); err != nil {
		return fmt.Errorf("download kernel: %w", err)
	}
	defer os.Remove(kernel.file.Name())
	if err := os.MkdirAll(store.kernelsDirectory, 0o755); err != nil {
		return err
	}
	if err := os.Rename(kernel.file.Name(), target); err != nil {
		return err
	}
	return os.Chmod(target, 0o644)
}

// HasKernel reports whether one kernel is stored.
func (store *ImageStore) HasKernel(kernelSHA256 string) (bool, error) {
	_, err := os.Stat(store.kernelFile(kernelSHA256))
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	return err == nil, err
}

// OpenKernel opens one stored kernel.
func (store *ImageStore) OpenKernel(kernelSHA256 string) (*os.File, error) {
	if !validSHA256(kernelSHA256) {
		return nil, fmt.Errorf("invalid kernel digest %q", kernelSHA256)
	}
	file, err := os.Open(store.kernelFile(kernelSHA256))
	if errors.Is(err, os.ErrNotExist) {
		return nil, ErrNotFound
	}
	return file, err
}

// StoreKernel stores one kernel from source after its digest matches.
func (store *ImageStore) StoreKernel(kernelSHA256 string, source io.Reader) error {
	if !validSHA256(kernelSHA256) {
		return fmt.Errorf("invalid kernel digest %q", kernelSHA256)
	}
	kernel := &temporaryFileDestination{directory: store.directory}
	writer, err := kernel.open(context.Background(), 0)
	if err != nil {
		return err
	}
	defer kernel.discard()

	hash := sha256.New()
	if _, err := io.Copy(io.MultiWriter(writer, hash), source); err != nil {
		return err
	}
	if !strings.EqualFold(hex.EncodeToString(hash.Sum(nil)), kernelSHA256) {
		return fmt.Errorf("%w: received kernel digest mismatch", ErrImageIntegrity)
	}
	if err := kernel.commit(); err != nil {
		return err
	}
	if err := os.MkdirAll(store.kernelsDirectory, 0o755); err != nil {
		return err
	}
	if err := os.Rename(kernel.file.Name(), store.kernelFile(kernelSHA256)); err != nil {
		return err
	}
	return os.Chmod(store.kernelFile(kernelSHA256), 0o644)
}

// importRootFileSystem downloads the root file system straight into a new ZFS
// volume and snapshots it. Every failure destroys the volume, so no half-written
// base image can be cloned.
func (store *ImageStore) importRootFileSystem(ctx context.Context, imageReference, rootfsURL, expectedDigest string) error {
	volume := &volumeDestination{store: store, imageReference: imageReference}
	if err := download(ctx, store.httpClient, rootfsURL, expectedDigest, volume, store.logger); err != nil {
		return fmt.Errorf("download root file system: %w", err)
	}
	if err := platform.Run(ctx, "zfs", "snapshot", store.pool.baseSnapshot(imageReference)); err != nil {
		volume.destroy()
		return err
	}
	return nil
}

// volumeDestination downloads into a new ZFS volume for one image.
type volumeDestination struct {
	store          *ImageStore
	imageReference string
	device         *os.File
}

// imageVolumeSizeMiB is the smallest whole MiB that holds an image. The import writes only the image bytes.
func imageVolumeSizeMiB(sizeBytes int64) int64 {
	return (sizeBytes + (1 << bytesToMiBShift) - 1) >> bytesToMiBShift
}

func (destination *volumeDestination) open(ctx context.Context, sizeBytes int64) (io.Writer, error) {
	dataset := destination.store.pool.baseDataset(destination.imageReference)
	if err := platform.Run(ctx, "zfs", "create", "-V", fmt.Sprintf("%dM", imageVolumeSizeMiB(sizeBytes)), "-o", "volblocksize="+imageVolumeBlockSize, dataset); err != nil {
		return nil, err
	}
	devicePath := destination.store.baseImageDevicePath(destination.imageReference)
	if _, err := waitForBlockDevice(devicePath); err != nil {
		destination.destroy()
		return nil, err
	}
	device, err := os.OpenFile(devicePath, os.O_WRONLY, 0)
	if err != nil {
		destination.destroy()
		return nil, err
	}
	destination.device = device
	return &SparseWriter{File: device}, nil
}

func (destination *volumeDestination) commit() error {
	if err := destination.device.Sync(); err != nil {
		return err
	}
	return destination.device.Close()
}

func (destination *volumeDestination) discard() {
	if destination.device != nil {
		destination.device.Close()
		destination.device = nil
	}
	destination.destroy()
}

func (destination *volumeDestination) destroy() {
	_ = platform.Run(context.Background(), "zfs", "destroy", "-r", destination.store.pool.baseDataset(destination.imageReference))
}

// baseImageDevicePath is the block device of one base image volume.
func (store *ImageStore) baseImageDevicePath(imageReference string) string {
	return "/dev/zvol/" + store.pool.baseDataset(imageReference)
}

// newImageHTTPClient returns a client with timeouts suited to large artifacts.
func newImageHTTPClient() *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DialContext = (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext
	transport.TLSHandshakeTimeout = 10 * time.Second
	transport.ResponseHeaderTimeout = 30 * time.Second
	transport.IdleConnTimeout = 90 * time.Second
	return &http.Client{Transport: transport, Timeout: downloadTimeout}
}

// SetImagePolicies atomically records the complete desired image policy set.
func (store *ImageStore) SetImagePolicies(ctx context.Context, images []vm.Image) error {
	if err := ctx.Err(); err != nil {
		return err
	}

	seen := make(map[string]struct{}, len(images))
	for _, image := range images {
		if _, found := seen[image.Name]; found {
			return fmt.Errorf("duplicate image reference %q", image.Name)
		}
		seen[image.Name] = struct{}{}
	}
	return writeJSONFile(store.policiesFile, images, 0o600)
}

// ImagePolicies returns the desired image policy set.
func (store *ImageStore) ImagePolicies(ctx context.Context) ([]vm.Image, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}

	data, err := os.ReadFile(store.policiesFile)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}

	var images []vm.Image
	if err := json.Unmarshal(data, &images); err != nil {
		return nil, fmt.Errorf("decode image policies: %w", err)
	}
	return images, nil
}

// EnsureImage downloads and verifies one compatible image.
func (store *ImageStore) EnsureImage(ctx context.Context, image vm.Image) error {
	if image.Architecture != runtime.GOARCH {
		return nil
	}
	return store.ensureImage(ctx, image.Name, image)
}

// RecordImageUse records a successful virtual machine start.
func (store *ImageStore) RecordImageUse(imageReference string, now time.Time) error {
	path := filepath.Join(store.imageDirectory(imageReference), lastUsedFileName)
	return writeJSONFile(path, now.UTC(), 0o640)
}

// PruneImages removes idle images that are not retained by policy.
func (store *ImageStore) PruneImages(ctx context.Context, policies []vm.Image, now time.Time, maximumIdle time.Duration) error {
	retained := make(map[string]bool, len(policies))
	for _, policy := range policies {
		retained[policy.Name] = policy.CacheImage && policy.Architecture == runtime.GOARCH
	}

	entries, err := os.ReadDir(store.directory)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if !entry.IsDir() || retained[entry.Name()] {
			continue
		}
		if err := store.pruneImage(ctx, entry.Name(), now, maximumIdle); err != nil {
			return err
		}
	}
	return nil
}

// pruneImage removes one image that has been idle for longer than maximumIdle.
// An image with a VM disk cloned from it is left alone.
func (store *ImageStore) pruneImage(ctx context.Context, imageReference string, now time.Time, maximumIdle time.Duration) error {
	lastUsed, err := store.imageLastUsed(imageReference)
	if err != nil {
		return err
	}
	if now.Sub(lastUsed) < maximumIdle {
		return nil
	}

	err = store.deleteImage(ctx, imageReference)
	if errors.Is(err, ErrInUse) {
		return nil
	}
	return err
}

// PruneKernels removes kernels idle for longer than maximumIdle that no stored
// image and no listed VM uses.
func (store *ImageStore) PruneKernels(usedKernelSHA256s []string, now time.Time, maximumIdle time.Duration) error {
	images, err := os.ReadDir(store.directory)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	for _, image := range images {
		if !image.IsDir() {
			continue
		}
		manifest, found, err := store.loadImageManifest(image.Name())
		if err != nil {
			return err
		}
		if found {
			usedKernelSHA256s = append(usedKernelSHA256s, manifest.KernelSHA256)
		}
	}

	kernels, err := os.ReadDir(store.kernelsDirectory)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	for _, kernel := range kernels {
		if slices.ContainsFunc(usedKernelSHA256s, func(used string) bool { return strings.EqualFold(used, kernel.Name()) }) {
			continue
		}
		information, err := kernel.Info()
		if err != nil {
			return err
		}
		if now.Sub(information.ModTime()) < maximumIdle {
			continue
		}
		if err := os.Remove(store.kernelFile(kernel.Name())); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	return nil
}

// imageLastUsed reports when an image was last started from. It falls back to
// the manifest time, then the directory time, so an image with no recorded use
// still ages out.
func (store *ImageStore) imageLastUsed(imageReference string) (time.Time, error) {
	data, err := os.ReadFile(filepath.Join(store.imageDirectory(imageReference), lastUsedFileName))
	if err == nil {
		var lastUsed time.Time
		if json.Unmarshal(data, &lastUsed) == nil && !lastUsed.IsZero() {
			return lastUsed, nil
		}
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return time.Time{}, err
	}

	information, err := os.Stat(store.manifestFile(imageReference))
	if errors.Is(err, os.ErrNotExist) {
		// Use directory age for incomplete images.
		directory, directoryErr := os.Stat(store.imageDirectory(imageReference))
		if directoryErr != nil {
			return time.Time{}, nil
		}
		return directory.ModTime(), nil
	}
	if err != nil {
		return time.Time{}, err
	}
	return information.ModTime(), nil
}
