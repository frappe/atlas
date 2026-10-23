package patches

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/frappe/atlas/metal/internal/platform"
)

func initializeSleepMigration(baseDirectory string) error {
	for _, name := range []string{"source.json", "destination.json"} {
		paths, err := filepath.Glob(filepath.Join(baseDirectory, "machines", "*", "migration", name))
		if err != nil {
			return err
		}
		for _, path := range paths {
			if err := upgradeSleepMigrationRecord(path); err != nil {
				return err
			}
		}
	}
	return nil
}

func upgradeSleepMigrationRecord(path string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var record map[string]json.RawMessage
	if err := json.Unmarshal(data, &record); err != nil {
		return fmt.Errorf("decode %s: %w", path, err)
	}
	var version int
	if err := json.Unmarshal(record["schema_version"], &version); err != nil {
		return fmt.Errorf("read %s schema version: %w", path, err)
	}
	if version != 2 && version != 3 {
		return fmt.Errorf("read %s: unsupported migration schema %d", path, version)
	}
	record["schema_version"] = json.RawMessage("3")
	data, err = json.MarshalIndent(record, "", "  ")
	if err != nil {
		return err
	}
	return platform.WriteFile(path, data, 0o600)
}
