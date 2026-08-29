package storage

import (
	"os"
	"path/filepath"
	"testing"
)

func TestNames(t *testing.T) {
	l := NewLVM("metalvg", "/imgs")
	if got := diskName("abc"); got != "vm-abc" {
		t.Errorf("diskName = %q", got)
	}
	if got := baseName("ubuntu"); got != "base-ubuntu" {
		t.Errorf("baseName = %q", got)
	}
	if got := vmTag("abc"); got != "metal-vm-abc" {
		t.Errorf("vmTag = %q", got)
	}
	if got := l.devPath("vm-abc"); got != "/dev/metalvg/vm-abc" {
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
