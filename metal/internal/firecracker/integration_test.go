//go:build integration

// Boots a real microVM and SSHes into it. Requires root, KVM, firecracker +
// jailer installed, and a prepared host (see docs/testing.md). Run with:
//
//	sudo -E go test -tags integration -run TestBootAndSSH -v ./internal/firecracker/
package firecracker

import (
	"context"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/systemd"
	"github.com/frappe/atlas/metal/internal/vm"
)

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func TestBootAndSSH(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("requires root (jailer)")
	}
	image := os.Getenv("METAL_IMAGE")
	pubPath := os.Getenv("METAL_SSH_PUB")
	privPath := os.Getenv("METAL_SSH_KEY")
	if image == "" || pubPath == "" || privPath == "" {
		t.Skip("set METAL_IMAGE, METAL_SSH_PUB, METAL_SSH_KEY")
	}
	pub, err := os.ReadFile(pubPath)
	if err != nil {
		t.Fatal(err)
	}

	ctx := context.Background()
	units, err := systemd.Connect(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer units.Close()

	d := New(DefaultConfig(),
		units,
		storage.NewZFS(env("METAL_POOL", "metal"), env("METAL_KERNEL_DIR", "/var/lib/metal/kernels")),
		network.NewLinux(),
	)

	m, err := d.Create(ctx, vm.Spec{
		VCPUs: 1, MemMiB: 256, DiskMiB: 0,
		Image:   vm.ImageRef{Name: image},
		Network: vm.NetworkRef{Name: "default"},
		SSHKeys: []string{strings.TrimSpace(string(pub))},
	})
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	t.Cleanup(func() { _ = m.Destroy(context.Background()) })

	if err := m.Start(ctx); err != nil {
		t.Fatalf("start: %v", err)
	}

	// SSH via the VM's netns; the guest IP is fixed (see network/linux.go).
	// The CI rootfs logs in as 'root'; the MMDS shim installs the key there.
	netns := "metal-" + m.ID()
	user := env("METAL_SSH_USER", "root")
	var out []byte
	for range 30 {
		out, err = exec.CommandContext(ctx, "ip", "netns", "exec", netns,
			"ssh", "-i", privPath,
			"-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=3",
			user+"@172.16.0.2", "echo", "metal-ok").CombinedOutput()
		if err == nil && strings.Contains(string(out), "metal-ok") {
			return // success
		}
		time.Sleep(2 * time.Second)
	}
	t.Fatalf("ssh never succeeded: %v: %s", err, out)
}
