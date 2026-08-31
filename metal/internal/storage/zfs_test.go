package storage

import (
	"os"
	"path/filepath"
	"testing"
)

func TestNames(t *testing.T) {
	z := NewZFS("metal", "/imgs")
	if got := z.baseDataset("ubuntu"); got != "metal/base/ubuntu" {
		t.Errorf("baseDataset = %q", got)
	}
	if got := z.baseSnapshot("ubuntu"); got != "metal/base/ubuntu@ready" {
		t.Errorf("baseSnapshot = %q", got)
	}
	if got := z.vmDataset("abc"); got != "metal/vms/abc" {
		t.Errorf("vmDataset = %q", got)
	}
	if got := z.devPath("abc"); got != "/dev/zvol/metal/vms/abc" {
		t.Errorf("devPath = %q", got)
	}
}

func TestBootArgs(t *testing.T) {
	dir := t.TempDir()
	if got := bootArgs(dir); got != defaultBootArgs {
		t.Errorf("default = %q", got)
	}
	if err := os.WriteFile(filepath.Join(dir, "boot-args"), []byte("custom args\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := bootArgs(dir); got != "custom args" {
		t.Errorf("override = %q", got)
	}
}
