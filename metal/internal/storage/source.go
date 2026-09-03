package storage

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/frappe/atlas/metal/internal/hostcmd"
	"github.com/frappe/atlas/metal/internal/vm"
)

var errRetryableDownload = errors.New("retryable download failure")

const (
	downloadAttempts  = 5
	downloadBaseDelay = 2 * time.Second
	downloadTimeout   = 30 * time.Minute
)

type imageManifest struct {
	RootfsSHA256 string `json:"rootfs_sha256,omitempty"`
	KernelSHA256 string `json:"kernel_sha256,omitempty"`
	Architecture string `json:"architecture"`
	Local        bool   `json:"local,omitempty"`
}

func (store *ImageStore) ensureImage(ctx context.Context, imageReference string, image vm.ImageRef) error {
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

	completed := found
	if !found {
		defer func() {
			if completed {
				return
			}
			_ = os.RemoveAll(store.imageDirectory(imageReference))
			_ = hostcmd.Run(context.Background(), "zfs", "destroy", "-r", store.pool.baseDataset(imageReference))
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

func hasImageSource(image vm.ImageRef) bool {
	return image.RootfsURL != "" || image.KernelURL != "" || image.RootfsSHA256 != "" ||
		image.KernelSHA256 != "" || image.Architecture != ""
}

func manifestForImage(image vm.ImageRef) (imageManifest, error) {
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

func validSHA256(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

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

func (store *ImageStore) saveImageManifest(imageReference string, manifest imageManifest) error {
	data, err := json.Marshal(manifest)
	if err != nil {
		return err
	}
	data = append(data, '\n')

	directory := store.imageDirectory(imageReference)
	if err := os.MkdirAll(directory, 0o755); err != nil {
		return err
	}
	file, err := os.CreateTemp(directory, ".manifest-*")
	if err != nil {
		return err
	}
	temporaryPath := file.Name()
	defer os.Remove(temporaryPath)

	if err := file.Chmod(0o644); err != nil {
		file.Close()
		return err
	}
	if _, err := file.Write(data); err != nil {
		file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryPath, store.manifestFile(imageReference)); err != nil {
		return err
	}

	directoryFile, err := os.Open(directory)
	if err != nil {
		return err
	}
	defer directoryFile.Close()
	return directoryFile.Sync()
}

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

	kernel, err := download(ctx, store.httpClient, store.directory, kernelURL, expectedDigest)
	if err != nil {
		return fmt.Errorf("download kernel: %w", err)
	}
	defer os.Remove(kernel)
	if err := os.MkdirAll(store.imageDirectory(imageReference), 0o755); err != nil {
		return err
	}
	if err := os.Rename(kernel, target); err != nil {
		return err
	}
	return os.Chmod(target, 0o644)
}

func (store *ImageStore) importRootFileSystem(ctx context.Context, imageReference, rootfsURL, expectedDigest string) error {
	rootfs, err := download(ctx, store.httpClient, store.directory, rootfsURL, expectedDigest)
	if err != nil {
		return fmt.Errorf("download root file system: %w", err)
	}
	defer os.Remove(rootfs)

	info, err := os.Stat(rootfs)
	if err != nil {
		return err
	}
	sizeMiB := info.Size()>>20 + 64
	if err := hostcmd.Run(ctx, "zfs", "create", "-V", fmt.Sprintf("%dM", sizeMiB), "-o", "volblocksize=16k", store.pool.baseDataset(imageReference)); err != nil {
		return err
	}
	rollback := func() {
		_ = hostcmd.Run(context.Background(), "zfs", "destroy", "-r", store.pool.baseDataset(imageReference))
	}
	if _, err := waitForBlockDevice(store.baseImageDevicePath(imageReference)); err != nil {
		rollback()
		return err
	}
	if err := hostcmd.Run(ctx, "dd", "if="+rootfs, "of="+store.baseImageDevicePath(imageReference), "bs=4M", "conv=sparse,fsync", "status=none"); err != nil {
		rollback()
		return err
	}
	if err := hostcmd.Run(ctx, "zfs", "snapshot", store.pool.baseSnapshot(imageReference)); err != nil {
		rollback()
		return err
	}
	return nil
}

func (store *ImageStore) baseImageDevicePath(imageReference string) string {
	return "/dev/zvol/" + store.pool.baseDataset(imageReference)
}

func newImageHTTPClient() *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DialContext = (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext
	transport.TLSHandshakeTimeout = 10 * time.Second
	transport.ResponseHeaderTimeout = 30 * time.Second
	transport.IdleConnTimeout = 90 * time.Second
	return &http.Client{Transport: transport, Timeout: downloadTimeout}
}

func download(ctx context.Context, client *http.Client, directory, source, expectedDigest string) (string, error) {
	if _, err := parseImageURL(source); err != nil {
		return "", err
	}
	if err := os.MkdirAll(directory, 0o755); err != nil {
		return "", err
	}

	redactedSource := redactURL(source)
	var lastErr error
	for attempt := 1; attempt <= downloadAttempts; attempt++ {
		if attempt > 1 {
			delay := downloadBaseDelay << (attempt - 2)
			log.Printf("storage: download %s failed (attempt %d/%d), retry in %s: %v", redactedSource, attempt-1, downloadAttempts, delay, lastErr)
			select {
			case <-ctx.Done():
				return "", ctx.Err()
			case <-time.After(delay):
			}
		}
		path, err := downloadOnce(ctx, client, directory, source, expectedDigest)
		if err == nil {
			return path, nil
		}
		if !errors.Is(err, errRetryableDownload) {
			return "", fmt.Errorf("download %s: %w", redactedSource, err)
		}
		lastErr = err
	}
	return "", fmt.Errorf("download %s after %d attempts: %w", redactedSource, downloadAttempts, lastErr)
}

func downloadOnce(ctx context.Context, client *http.Client, directory, source, expectedDigest string) (string, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, source, nil)
	if err != nil {
		return "", fmt.Errorf("create request")
	}
	result, err := client.Do(request)
	if err != nil {
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		return "", fmt.Errorf("%w: request failed", errRetryableDownload)
	}
	defer result.Body.Close()
	if result.StatusCode != http.StatusOK {
		statusError := fmt.Errorf("HTTP status %d", result.StatusCode)
		if result.StatusCode == http.StatusRequestTimeout || result.StatusCode == http.StatusTooManyRequests || result.StatusCode >= 500 {
			return "", fmt.Errorf("%w: %v", errRetryableDownload, statusError)
		}
		return "", statusError
	}

	file, err := os.CreateTemp(directory, "download-*")
	if err != nil {
		return "", err
	}
	path := file.Name()
	remove := func() {
		file.Close()
		os.Remove(path)
	}

	hash := sha256.New()
	written, err := io.Copy(io.MultiWriter(file, hash), result.Body)
	if err == nil && result.ContentLength >= 0 && written != result.ContentLength {
		err = fmt.Errorf("truncated response")
	}
	if err == nil && hex.EncodeToString(hash.Sum(nil)) != expectedDigest {
		err = fmt.Errorf("%w: SHA-256 digest mismatch", ErrImageIntegrity)
	}
	if err != nil {
		remove()
		return "", err
	}
	if err := file.Sync(); err != nil {
		remove()
		return "", err
	}
	if err := file.Close(); err != nil {
		os.Remove(path)
		return "", err
	}
	return path, nil
}

func verifyFileSHA256(path, expectedDigest string) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()

	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return err
	}
	if hex.EncodeToString(hash.Sum(nil)) != expectedDigest {
		return fmt.Errorf("SHA-256 digest mismatch")
	}
	return nil
}

func parseImageURL(source string) (*url.URL, error) {
	parsed, err := url.ParseRequestURI(source)
	if err != nil || parsed.Host == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return nil, fmt.Errorf("invalid image URL")
	}
	return parsed, nil
}

func redactURL(source string) string {
	parsed, err := url.Parse(source)
	if err != nil {
		return "invalid image URL"
	}
	parsed.RawQuery = ""
	parsed.Fragment = ""
	parsed.User = nil
	return parsed.String()
}
