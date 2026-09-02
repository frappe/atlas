package storage

import (
	"context"
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

// On one filesystem, LinkOrReflink must hard-link (share the inode), not copy,
// so staging a large memory file into a chroot is instant.
func TestLinkOrReflink(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "src")
	dst := filepath.Join(dir, "dst")
	if err := os.WriteFile(src, []byte("guest-ram"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := LinkOrReflink(context.Background(), src, dst); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(dst)
	if err != nil || string(got) != "guest-ram" {
		t.Fatalf("dst = %q, err %v", got, err)
	}
	si, err := os.Stat(src)
	if err != nil {
		t.Fatal(err)
	}
	di, err := os.Stat(dst)
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(si, di) {
		t.Error("expected a hard link (same inode), got a copy")
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
