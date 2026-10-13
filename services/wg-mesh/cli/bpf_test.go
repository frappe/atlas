package main

import (
	"bytes"
	"os"
	"testing"

	"github.com/cilium/ebpf"
)

// A host upgrade reuses each pinned map with the same name, and loadBPF stops
// on a changed layout. CI passes the object of the base branch in
// BPF_BASELINE_OBJECT, so a layout change fails before it reaches a host.
func TestPinnedMapsKeepTheirLayout(t *testing.T) {
	baselinePath := os.Getenv("BPF_BASELINE_OBJECT")
	if baselinePath == "" {
		t.Skip("BPF_BASELINE_OBJECT names no baseline object")
	}

	baseline, err := ebpf.LoadCollectionSpec(baselinePath)
	if err != nil {
		t.Fatalf("read the baseline object: %v", err)
	}
	current, err := ebpf.LoadCollectionSpecFromReader(bytes.NewReader(bpfObject))
	if err != nil {
		t.Fatalf("read the embedded object: %v", err)
	}

	for name, currentMap := range current.Maps {
		baselineMap, found := baseline.Maps[name]
		if !found {
			continue
		}
		if currentMap.Type != baselineMap.Type || currentMap.KeySize != baselineMap.KeySize ||
			currentMap.ValueSize != baselineMap.ValueSize || currentMap.MaxEntries != baselineMap.MaxEntries ||
			currentMap.Flags != baselineMap.Flags {
			t.Errorf("map %s changed layout from %v to %v; add a map with a new name instead", name, baselineMap, currentMap)
		}
	}
}
