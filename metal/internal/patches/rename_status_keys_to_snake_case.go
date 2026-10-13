package patches

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/frappe/atlas/metal/internal/platform"
)

// renameStatusKeysToSnakeCase renames the network_interface and disk keys in each VM status.json to snake_case.
func renameStatusKeysToSnakeCase(baseDirectory string) error {
	paths, err := filepath.Glob(filepath.Join(baseDirectory, "machines", "*", "status.json"))
	if err != nil {
		return err
	}

	renames := map[string]map[string]string{
		"network_interface": {
			"NetworkNamespacePath": "network_namespace_path",
			"TapName":              "tap_name",
			"MACAddress":           "mac_address",
			"GuestIPAddress":       "guest_ip_address",
			"GatewayIPAddress":     "gateway_ip_address",
		},
		"disk": {"SizeMiB": "size_mib", "UsedMiB": "used_mib"},
	}
	for _, path := range paths {
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		var record map[string]json.RawMessage
		if err := json.Unmarshal(data, &record); err != nil {
			return fmt.Errorf("read %s: %w", path, err)
		}

		for name, keys := range renames {
			var object map[string]json.RawMessage
			if raw, found := record[name]; !found || json.Unmarshal(raw, &object) != nil {
				continue
			}
			for oldKey, newKey := range keys {
				if value, found := object[oldKey]; found {
					object[newKey] = value
					delete(object, oldKey)
				}
			}
			record[name], _ = json.Marshal(object)
		}

		if data, err = json.MarshalIndent(record, "", "  "); err != nil {
			return err
		}
		if err := platform.WriteFile(path, data, 0o640); err != nil {
			return err
		}
	}

	return nil
}
