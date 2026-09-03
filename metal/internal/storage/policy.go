package storage

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"github.com/frappe/atlas/metal/internal/vm"
)

// SetImagePolicies atomically records the complete desired image policy set.
func (store *ImageStore) SetImagePolicies(ctx context.Context, images []vm.ImageRef) error {
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
func (store *ImageStore) ImagePolicies(ctx context.Context) ([]vm.ImageRef, error) {
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

	var images []vm.ImageRef
	if err := json.Unmarshal(data, &images); err != nil {
		return nil, fmt.Errorf("decode image policies: %w", err)
	}
	return images, nil
}

// EnsureImage downloads and verifies one compatible image.
func (store *ImageStore) EnsureImage(ctx context.Context, image vm.ImageRef) error {
	if image.Architecture != runtime.GOARCH {
		return nil
	}
	return store.ensureImage(ctx, image.Name, image)
}

// RecordImageUse records a successful virtual machine start.
func (store *ImageStore) RecordImageUse(imageReference string, now time.Time) error {
	path := filepath.Join(store.imageDirectory(imageReference), "last-used")
	return writeJSONFile(path, now.UTC(), 0o640)
}

// PruneImages removes idle images that are not retained by policy.
func (store *ImageStore) PruneImages(ctx context.Context, policies []vm.ImageRef, now time.Time, maximumIdle time.Duration) error {
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

func (store *ImageStore) imageLastUsed(imageReference string) (time.Time, error) {
	data, err := os.ReadFile(filepath.Join(store.imageDirectory(imageReference), "last-used"))
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
	if err != nil {
		return time.Time{}, err
	}
	return information.ModTime(), nil
}
