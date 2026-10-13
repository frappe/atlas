package patches

import (
	"bytes"
	"testing"
)

func TestConvertRunsEachPatchInOrderAndSkipsOtherMaps(t *testing.T) {
	appendVersion := func(version byte) func(string, []byte, []byte) ([]byte, []byte, error) {
		return func(_ string, key, value []byte) ([]byte, []byte, error) {
			return key, append(bytes.Clone(value), version), nil
		}
	}
	list := []Patch{
		{Maps: []string{"remote_vms"}, Convert: appendVersion(1)},
		{Maps: []string{"local_vms"}, Convert: appendVersion(2)},
		{Maps: []string{"remote_vms"}, Convert: appendVersion(3)},
	}

	_, value, err := convert(list, 0, 3, "remote_vms", []byte{0}, []byte{0})
	if err != nil || !bytes.Equal(value, []byte{0, 1, 3}) {
		t.Fatalf("value = %v, %v; want [0 1 3]", value, err)
	}

	_, value, err = convert(list, 1, 3, "remote_vms", []byte{0}, []byte{0})
	if err != nil || !bytes.Equal(value, []byte{0, 3}) {
		t.Fatalf("value from v1 = %v, %v; want [0 3]", value, err)
	}

	if changed := changedMaps(list, 1, 3); len(changed) != 2 || !changed["local_vms"] || !changed["remote_vms"] {
		t.Fatalf("changed maps from v1 = %v", changed)
	}
}

func TestADroppedEntryStopsTheChain(t *testing.T) {
	list := []Patch{
		{Maps: []string{"remote_vms"}, Convert: func(string, []byte, []byte) ([]byte, []byte, error) {
			return nil, nil, nil
		}},
		{Maps: []string{"remote_vms"}, Convert: func(string, []byte, []byte) ([]byte, []byte, error) {
			t.Fatal("a later patch got a dropped entry")
			return nil, nil, nil
		}},
	}

	if key, _, err := convert(list, 0, 2, "remote_vms", []byte{0}, []byte{0}); key != nil || err != nil {
		t.Fatalf("key = %v, error = %v; want a dropped entry", key, err)
	}
}
