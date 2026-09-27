// Package datum exports host and per-VM metrics to the datum telemetry service.
package datum

import (
	"encoding/json"
	"fmt"
	"os"
)

// TokenBundle holds the write tokens Atlas minted for this host and its VMs.
type TokenBundle struct {
	Host string            `json:"host"`
	VMs  map[string]string `json:"vms"`
}

// ReadTokenBundle reads the token bundle Atlas shipped to path.
func ReadTokenBundle(path string) (TokenBundle, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return TokenBundle{}, fmt.Errorf("read token bundle: %w", err)
	}

	var bundle TokenBundle
	if err := json.Unmarshal(data, &bundle); err != nil {
		return TokenBundle{}, fmt.Errorf("parse token bundle: %w", err)
	}
	return bundle, nil
}
