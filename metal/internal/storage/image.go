package storage

import (
	"context"
	"errors"
	"os"
	"strings"

	"github.com/frappe/atlas/metal/internal/hostcmd"
)

func (store *ImageStore) deleteImage(ctx context.Context, imageReference string) error {
	lock := store.imageLock(imageReference)
	lock.Lock()
	defer lock.Unlock()

	if err := store.removeWarmImages(ctx, imageReference); err != nil {
		return err
	}
	err := hostcmd.Run(ctx, "zfs", "destroy", "-r", store.pool.baseDataset(imageReference))
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

func (store *ImageStore) removeIncompleteImage(ctx context.Context, imageReference string) {
	_ = hostcmd.Run(ctx, "zfs", "destroy", "-r", store.pool.baseDataset(imageReference))
	_ = os.RemoveAll(store.imageDirectory(imageReference))
}

func ignoreNotFound(err error) error {
	if errors.Is(err, ErrNotFound) {
		return nil
	}
	return err
}
