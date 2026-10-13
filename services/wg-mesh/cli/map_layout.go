package main

import (
	"bytes"
	"debug/elf"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/cilium/ebpf"

	"github.com/frappe/atlas/services/wg-mesh/cli/patches"
)

const mapLayoutSection = "atlas_map_layout"

var mapLayoutVersion = mustReadMapLayoutVersion(bpfObject)

// readMapLayoutVersion reads map_layout_version from a BPF object. An object without it is version 0.
func readMapLayoutVersion(object []byte) (uint32, error) {
	file, err := elf.NewFile(bytes.NewReader(object))
	if err != nil {
		return 0, fmt.Errorf("read the BPF object: %w", err)
	}

	section := file.Section(mapLayoutSection)
	if section == nil {
		return 0, nil
	}
	data, err := section.Data()
	if err != nil || len(data) != 4 {
		return 0, fmt.Errorf("read the %s section: %d bytes, %v", mapLayoutSection, len(data), err)
	}

	return file.ByteOrder.Uint32(data), nil
}

func mustReadMapLayoutVersion(object []byte) uint32 {
	version, err := readMapLayoutVersion(object)
	if err != nil {
		panic(err)
	}
	if version != patches.LatestVersion() {
		panic(fmt.Sprintf("map layout version %d needs %d patches, found %d", version, version, patches.LatestVersion()))
	}

	return version
}

// mapDirectory holds the pinned maps of one layout version. Version 0 keeps the original paths.
func mapDirectory(version uint32) string {
	if version == 0 {
		return pinDirectory
	}

	return filepath.Join(pinDirectory, "maps", fmt.Sprintf("v%d", version))
}

// readInstalledMapLayoutVersion returns the oldest layout with a pinned config map. A commit removes the older
// layouts, so a newer layout beside one is a migration that did not finish. A host without the mesh gets the
// layout of this build. Glob fails only on a malformed pattern.
func readInstalledMapLayoutVersion() uint32 {
	if _, err := os.Stat(filepath.Join(mapDirectory(0), "config")); err == nil {
		return 0
	}

	configs, _ := filepath.Glob(filepath.Join(pinDirectory, "maps", "v*", "config"))
	installed, isInstalled := uint32(0), false
	for _, config := range configs {
		version, err := strconv.ParseUint(strings.TrimPrefix(filepath.Base(filepath.Dir(config)), "v"), 10, 32)
		if err == nil && (!isInstalled || uint32(version) < installed) {
			installed, isInstalled = uint32(version), true
		}
	}
	if !isInstalled {
		return mapLayoutVersion
	}

	return installed
}

// migrateChangedMaps copies each changed map into the new layout through the patches. A repeat is safe, so
// configure runs it again after the hook switch for entries learned in between.
func (candidate bpfCandidate) migrateChangedMaps() error {
	for name := range patches.ChangedMaps(candidate.installedVersion, mapLayoutVersion) {
		if err := migrateMap(name, candidate.installedVersion); err != nil {
			return fmt.Errorf("migrate map %s: %w", name, err)
		}
	}

	return nil
}

// migrateMap copies one map, unless only one of the two layouts has it.
func migrateMap(name string, installedVersion uint32) error {
	source, err := openPinnedMap(filepath.Join(mapDirectory(installedVersion), name))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	defer source.Close()

	destination, err := openPinnedMap(filepath.Join(mapDirectory(mapLayoutVersion), name))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	defer destination.Close()

	var key, value []byte
	iterator := source.Iterate()
	for iterator.Next(&key, &value) {
		newKey, newValue, err := patches.Convert(installedVersion, mapLayoutVersion, name, key, value)
		if err != nil {
			return err
		}
		if newKey == nil {
			continue
		}
		if err := destination.Put(newKey, newValue); err != nil {
			return err
		}
	}

	return iterator.Err()
}

// pinAgain adds a second pin. Pin on the map itself would move its pin.
func pinAgain(bpfMap *ebpf.Map, path string) error {
	clone, err := bpfMap.Clone()
	if err != nil {
		return err
	}
	defer clone.Close()

	return clone.Pin(path)
}

// removeObsoleteMaps removes the unused pins of this layout and every pin of older layouts. Attached programs
// hold their own map references.
func removeObsoleteMaps(active map[string]bool) error {
	if err := removeMapPins(mapDirectory(mapLayoutVersion), active); err != nil || mapLayoutVersion == 0 {
		return err
	}
	if err := removeMapPins(mapDirectory(0), nil); err != nil {
		return err
	}

	older, _ := filepath.Glob(filepath.Join(pinDirectory, "maps", "v*"))
	for _, directory := range older {
		if directory == mapDirectory(mapLayoutVersion) {
			continue
		}
		if err := os.RemoveAll(directory); err != nil {
			return err
		}
	}

	return nil
}

// removeMapPins removes the map pins in a directory that active does not name.
func removeMapPins(directory string, active map[string]bool) error {
	entries, err := os.ReadDir(directory)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if entry.IsDir() || active[entry.Name()] {
			continue
		}
		if err := os.Remove(filepath.Join(directory, entry.Name())); err != nil {
			return err
		}
	}

	return nil
}
