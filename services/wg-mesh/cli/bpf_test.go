package main

import (
	"bytes"
	"os"
	"testing"

	"github.com/cilium/ebpf"

	"github.com/frappe/atlas/services/wg-mesh/cli/patches"
)

// A pinned map that changes layout needs a patch. CI passes the base branch object in BPF_BASELINE_OBJECT.
func TestChangedMapLayoutsHavePatches(t *testing.T) {
	baselinePath := os.Getenv("BPF_BASELINE_OBJECT")
	if baselinePath == "" {
		t.Skip("BPF_BASELINE_OBJECT names no baseline object")
	}

	baselineObject, err := os.ReadFile(baselinePath)
	if err != nil {
		t.Fatalf("read the baseline object: %v", err)
	}
	baseline, err := ebpf.LoadCollectionSpecFromReader(bytes.NewReader(baselineObject))
	if err != nil {
		t.Fatalf("read the baseline object: %v", err)
	}
	baselineVersion, err := readMapLayoutVersion(baselineObject)
	if err != nil {
		t.Fatal(err)
	}
	current, err := ebpf.LoadCollectionSpecFromReader(bytes.NewReader(bpfObject))
	if err != nil {
		t.Fatalf("read the embedded object: %v", err)
	}

	if mapLayoutVersion < baselineVersion {
		t.Fatalf("map_layout_version went back from %d to %d; patches only move forward", baselineVersion, mapLayoutVersion)
	}

	changed := patches.ChangedMaps(baselineVersion, mapLayoutVersion)
	for name, baselineMap := range baseline.Maps {
		currentMap, found := current.Maps[name]
		if !found || hasSameLayout(baselineMap, currentMap) || changed[name] {
			continue
		}

		t.Errorf("map %s changed from %v to %v: bump map_layout_version and add a patch for it", name, baselineMap, currentMap)
	}
}

func hasSameLayout(first, second *ebpf.MapSpec) bool {
	return first.Type == second.Type && first.KeySize == second.KeySize && first.ValueSize == second.ValueSize &&
		first.MaxEntries == second.MaxEntries && first.Flags == second.Flags
}
