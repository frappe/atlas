// Package patches converts pinned map entries between map layout versions. all[i] moves version i to i+1.
// configure runs them in order, so a host can skip versions. Add each patch in its own file, such as v1.go,
// append it to all, and never change a released one.
package patches

import (
	"fmt"
	"slices"
)

// Patch converts the maps that change in one layout version. Other maps keep their kernel object.
type Patch struct {
	Maps []string

	// Convert returns one entry in the next layout. A nil key drops the entry.
	Convert func(mapName string, key, value []byte) (newKey, newValue []byte, err error)
}

var all = []Patch{}

// LatestVersion is the layout version that the patches reach.
func LatestVersion() uint32 {
	return uint32(len(all))
}

// ChangedMaps returns the maps that the patches between two versions convert.
func ChangedMaps(from, to uint32) map[string]bool {
	return changedMaps(all, from, to)
}

// Convert runs one entry through the patches between two versions. A nil key means a patch dropped it.
func Convert(from, to uint32, mapName string, key, value []byte) ([]byte, []byte, error) {
	return convert(all, from, to, mapName, key, value)
}

func changedMaps(list []Patch, from, to uint32) map[string]bool {
	changed := make(map[string]bool)
	for _, patch := range list[from:to] {
		for _, name := range patch.Maps {
			changed[name] = true
		}
	}

	return changed
}

func convert(list []Patch, from, to uint32, mapName string, key, value []byte) ([]byte, []byte, error) {
	for version := from; version < to; version++ {
		patch := list[version]
		if !slices.Contains(patch.Maps, mapName) {
			continue
		}

		var err error
		key, value, err = patch.Convert(mapName, key, value)
		if err != nil {
			return nil, nil, fmt.Errorf("patch v%d to v%d of map %s: %w", version, version+1, mapName, err)
		}
		if key == nil {
			return nil, nil, nil
		}
	}

	return key, value, nil
}
