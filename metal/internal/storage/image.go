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
	"strings"
	"time"

	"github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/vm"
)

const (
	// rootFileSystemSlackMiB is added to the volume that receives an image, so a
	// sparse write has room for file system overhead.
	rootFileSystemSlackMiB = 64

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
	case err == nil:
	case strings.Contains(err.Error(), "does not exist"):
		return ErrNotFound
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

// imageManifest records the content an image reference is bound to. Local marks
// an image built on this host, which has no URLs and no digests to verify.
type imageManifest struct {
	RootfsSHA256 string `json:"rootfs_sha256,omitempty"`
	KernelSHA256 string `json:"kernel_sha256,omitempty"`
	Architecture string `json:"architecture"`
	Local        bool   `json:"local,omitempty"`
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
	if found && storedManifest.Local {
		if hasImageSource(image) {
			return fmt.Errorf("%w: image reference %q identifies a local image", ErrImageConflict, imageReference)
		}
		complete, err := store.localImageArtifactsExist(ctx, imageReference)
		if err != nil {
			return err
		}
		if !complete {
			return fmt.Errorf("%w: local image %q is incomplete", ErrImageIntegrity, imageReference)
		}
		return nil
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
	artifactsExist, err := store.imageArtifactsExist(ctx, imageReference)
	if err != nil {
		return err
	}
	if !found && artifactsExist {
		return fmt.Errorf("%w: image reference %q has no manifest", ErrImageConflict, imageReference)
	}

	// An import that does not reach the manifest write leaves nothing behind.
	completed := found
	if !found {
		defer func() {
			if completed {
				return
			}
			_ = os.RemoveAll(store.imageDirectory(imageReference))
			_ = platform.Run(context.Background(), "zfs", "destroy", "-r", store.pool.baseDataset(imageReference))
		}()
	}

	if err := store.ensureKernel(ctx, imageReference, image.KernelURL, manifest.KernelSHA256); err != nil {
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

// hasImageSource reports whether a policy names remote image content.
func hasImageSource(image vm.Image) bool {
	return image.RootfsURL != "" || image.KernelURL != "" || image.RootfsSHA256 != "" ||
		image.KernelSHA256 != "" || image.Architecture != ""
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

// imageArtifactsExist reports whether any artifact of an image is on the host.
func (store *ImageStore) imageArtifactsExist(ctx context.Context, imageReference string) (bool, error) {
	exists, err := datasetExists(ctx, store.pool.baseDataset(imageReference))
	if err != nil || exists {
		return exists, err
	}

	_, err = os.Stat(store.kernelFile(imageReference))
	if err == nil {
		return true, nil
	}
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	return false, err
}

// localImageArtifactsExist reports whether a local image has every file it needs.
func (store *ImageStore) localImageArtifactsExist(ctx context.Context, imageReference string) (bool, error) {
	exists, err := datasetExists(ctx, store.pool.baseDataset(imageReference))
	if err != nil || !exists {
		return false, err
	}

	for _, path := range []string{
		store.kernelFile(imageReference),
		filepath.Join(store.imageDirectory(imageReference), "state"),
		filepath.Join(store.imageDirectory(imageReference), "mem"),
	} {
		if _, err := os.Stat(path); err != nil {
			if errors.Is(err, os.ErrNotExist) {
				return false, nil
			}
			return false, err
		}
	}
	return true, nil
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
	if !manifest.Local && (!validSHA256(manifest.RootfsSHA256) || !validSHA256(manifest.KernelSHA256)) {
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

// ensureKernel makes the image kernel present and verified. A stored kernel that
// fails verification is an integrity error, not a reason to download again.
func (store *ImageStore) ensureKernel(ctx context.Context, imageReference, kernelURL, expectedDigest string) error {
	target := store.kernelFile(imageReference)
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
	if err := os.MkdirAll(store.imageDirectory(imageReference), 0o755); err != nil {
		return err
	}
	if err := os.Rename(kernel.file.Name(), target); err != nil {
		return err
	}
	return os.Chmod(target, 0o644)
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

func (destination *volumeDestination) open(ctx context.Context, sizeBytes int64) (io.Writer, error) {
	sizeMiB := (sizeBytes+(1<<bytesToMiBShift)-1)>>bytesToMiBShift + rootFileSystemSlackMiB
	dataset := destination.store.pool.baseDataset(destination.imageReference)
	if err := platform.Run(ctx, "zfs", "create", "-V", fmt.Sprintf("%dM", sizeMiB), "-o", "volblocksize="+imageVolumeBlockSize, dataset); err != nil {
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
	return &sparseVolumeWriter{file: device}, nil
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
// An image still in use, or already gone, is left alone.
func (store *ImageStore) pruneImage(ctx context.Context, imageReference string, now time.Time, maximumIdle time.Duration) error {
	lastUsed, err := store.imageLastUsed(imageReference)
	if err != nil {
		return err
	}
	if now.Sub(lastUsed) < maximumIdle {
		return nil
	}

	err = store.deleteImage(ctx, imageReference)
	if errors.Is(err, ErrInUse) || errors.Is(err, ErrNotFound) {
		return nil
	}
	return err
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
