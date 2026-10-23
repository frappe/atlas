package patches

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/frappe/atlas/metal/internal/platform"
)

// renameComatoseSleepStateToComa rewrites the sleep state value "comatose" as
// "coma" in VM status records and in migration records, which carry the state of
// an offloaded checkpoint.
func renameComatoseSleepStateToComa(baseDirectory string) error {
	for _, pattern := range []string{
		filepath.Join(baseDirectory, "machines", "*", "status.json"),
		filepath.Join(baseDirectory, "machines", "*", "migration", "*.json"),
	} {
		paths, err := filepath.Glob(pattern)
		if err != nil {
			return err
		}
		for _, path := range paths {
			if err := renameComaInRecord(path); err != nil {
				return err
			}
		}
	}
	return nil
}

func renameComaInRecord(path string) error {
	information, err := os.Stat(path)
	if err != nil {
		return err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if !bytes.Contains(data, []byte(`"comatose"`)) {
		return nil
	}
	// UseNumber keeps 64-bit generations exact.
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	var record any
	if err := decoder.Decode(&record); err != nil {
		return fmt.Errorf("decode %s: %w", path, err)
	}
	renameComa(record)
	data, err = json.MarshalIndent(record, "", "  ")
	if err != nil {
		return fmt.Errorf("encode %s: %w", path, err)
	}
	return platform.WriteFile(path, data, information.Mode().Perm())
}

// renameComa replaces the old value of every sleep_state field and of the state
// field of a sleep checkpoint. No other state field holds "comatose".
func renameComa(value any) {
	switch typed := value.(type) {
	case map[string]any:
		for key, child := range typed {
			if (key == "sleep_state" || key == "state") && child == "comatose" {
				typed[key] = "coma"
				continue
			}
			renameComa(child)
		}
	case []any:
		for _, child := range typed {
			renameComa(child)
		}
	}
}
