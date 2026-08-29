package firecracker

import (
	"context"
	"errors"
	"testing"

	"github.com/frappe/atlas-neo/metal/internal/vm"
)

func TestLoadAndList(t *testing.T) {
	d := &Driver{cfg: testConfig(t.TempDir())}
	if err := d.cfg.writeVMConfig(vmConfig{ID: "aaa", UID: 100000, IP: "172.16.0.2"}); err != nil {
		t.Fatal(err)
	}
	if err := d.cfg.writeVMConfig(vmConfig{ID: "bbb", UID: 100001}); err != nil {
		t.Fatal(err)
	}

	got, err := d.Load(context.Background(), "aaa")
	if err != nil {
		t.Fatal(err)
	}
	if got.ID() != "aaa" {
		t.Errorf("Load id = %q", got.ID())
	}

	if _, err := d.Load(context.Background(), "missing"); !errors.Is(err, vm.ErrNotFound) {
		t.Errorf("Load missing err = %v, want ErrNotFound", err)
	}

	vms, err := d.List(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(vms) != 2 {
		t.Fatalf("List returned %d VMs", len(vms))
	}
}

func TestListEmpty(t *testing.T) {
	d := &Driver{cfg: testConfig(t.TempDir())}
	vms, err := d.List(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(vms) != 0 {
		t.Errorf("List returned %d VMs, want 0", len(vms))
	}
}
