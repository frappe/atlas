package storage

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

func TestRemoveWarmImagesContinuesPastInUseDisk(t *testing.T) {
	commandDirectory := t.TempDir()
	command := `#!/bin/sh
if [ "$1" = destroy ] && [ "$3" = metal/warm/a ]; then
	echo 'cannot destroy: snapshot has dependent clones' >&2
	exit 1
fi
`
	if err := os.WriteFile(filepath.Join(commandDirectory, "zfs"), []byte(command), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", commandDirectory+string(os.PathListSeparator)+os.Getenv("PATH"))

	store := NewStores(t.Context(), "metal", t.TempDir(), nil, telemetry.NewRegistry(nil)).Images
	for _, key := range []string{"a", "b"} {
		if err := os.MkdirAll(filepath.Join(store.imageDirectory("image"), "warm", key), 0o750); err != nil {
			t.Fatal(err)
		}
	}

	if err := store.RemoveWarmImages(t.Context(), "image"); !errors.Is(err, ErrInUse) {
		t.Fatalf("error = %v, want ErrInUse", err)
	}
	if _, err := os.Stat(filepath.Join(store.imageDirectory("image"), "warm", "a")); err != nil {
		t.Fatalf("in-use artifact was removed: %v", err)
	}
	if _, err := os.Stat(filepath.Join(store.imageDirectory("image"), "warm", "b")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("unused artifact remains: %v", err)
	}
}
