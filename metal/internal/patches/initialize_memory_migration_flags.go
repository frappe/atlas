package patches

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/frappe/atlas/metal/internal/platform"
)

func initializeMemoryMigrationFlags(baseDirectory string) error {
	for _, name := range []string{"source.json", "destination.json"} {
		paths, err := filepath.Glob(filepath.Join(baseDirectory, "machines", "*", "migration", name))
		if err != nil {
			return err
		}
		for _, path := range paths {
			if err := initializeMemoryMigrationFlag(path, name == "source.json"); err != nil {
				return err
			}
		}
	}
	return nil
}

func initializeMemoryMigrationFlag(path string, isSource bool) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read %s: %w", path, err)
	}
	var record map[string]json.RawMessage
	if err := json.Unmarshal(data, &record); err != nil {
		return fmt.Errorf("read %s: %w", path, err)
	}
	if record == nil {
		return fmt.Errorf("read %s: migration record must be an object", path)
	}
	if _, exists := record["has_memory_snapshot"]; exists {
		return nil
	}

	record["has_memory_snapshot"] = json.RawMessage("false")
	if isSource {
		// A locked source chooses its transfer mode when StopSource runs.
		record["has_memory_snapshot"] = json.RawMessage("null")
	}
	data, err = json.MarshalIndent(record, "", "  ")
	if err != nil {
		return fmt.Errorf("encode %s: %w", path, err)
	}
	if err := platform.WriteFile(path, data, 0o640); err != nil {
		return fmt.Errorf("write %s: %w", path, err)
	}
	return nil
}
