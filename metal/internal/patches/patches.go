// Package patches changes host state from an older metald, once per host, before metald reads it. Add each
// patch at the end of all, and give the patch, its file and its function one name that says what it changes,
// such as rename_status_keys_to_snake_case. Mandatory: a patch must be idempotent. Never change a released patch.
package patches

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"slices"

	"github.com/frappe/atlas/metal/internal/platform"
)

// Patch is one named change to the state under the metald base directory.
type Patch struct {
	Name  string
	Apply func(baseDirectory string) error
}

// all lists the patches in the order they run.
var all = []Patch{
	{Name: "rename_status_keys_to_snake_case", Apply: renameStatusKeysToSnakeCase},
	{Name: "initialize_memory_migration_flags", Apply: initializeMemoryMigrationFlags},
}

// Names returns the patch names in the order they run.
func Names() []string {
	names := make([]string, 0, len(all))
	for _, patch := range all {
		names = append(names, patch.Name)
	}
	return names
}

// Run applies each patch that patches.json does not list yet, and then lists it.
func Run(baseDirectory string, logger *slog.Logger) error {
	return run(all, baseDirectory, logger)
}

func run(list []Patch, baseDirectory string, logger *slog.Logger) error {
	path := filepath.Join(baseDirectory, "patches.json")
	var applied []string
	if data, err := os.ReadFile(path); err == nil {
		if err := json.Unmarshal(data, &applied); err != nil {
			return fmt.Errorf("read %s: %w", path, err)
		}
	} else if !errors.Is(err, fs.ErrNotExist) {
		return err
	}

	for _, patch := range list {
		if slices.Contains(applied, patch.Name) {
			continue
		}

		logger.Info("applying patch", "patch", patch.Name)
		if err := patch.Apply(baseDirectory); err != nil {
			return fmt.Errorf("patch %s: %w", patch.Name, err)
		}
		applied = append(applied, patch.Name)
		data, _ := json.Marshal(applied)
		if err := platform.WriteFile(path, data, 0o640); err != nil {
			return err
		}
	}

	return nil
}
