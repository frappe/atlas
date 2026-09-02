//go:build integration

// Boots real microVMs and drives them over SSH. Requires root, KVM, firecracker +
// jailer installed, and a prepared host (see docs/testing.md). Run with:
//
//	sudo -E go test -tags integration -v ./internal/firecracker/
//
// The tests share a host, so they run one at a time (each cleans up its VMs).
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

// skipUnlessHost skips unless the test runs as root with the image and ssh key
// env set. It returns the image ref and the public key contents.
func skipUnlessHost(t *testing.T) (image, pub string) {
	t.Helper()
	if os.Geteuid() != 0 {
		t.Skip("requires root (jailer)")
	}
	image = os.Getenv("METAL_IMAGE")
	pubPath := os.Getenv("METAL_SSH_PUB")
	if image == "" || pubPath == "" || os.Getenv("METAL_SSH_KEY") == "" {
		t.Skip("set METAL_IMAGE, METAL_SSH_PUB, METAL_SSH_KEY")
	}
	b, err := os.ReadFile(pubPath)
	if err != nil {
		t.Fatal(err)
	}
	return image, strings.TrimSpace(string(b))
}

func newDriver(t *testing.T) *Driver {
	t.Helper()
	units, err := systemd.Connect(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { units.Close() })
	return New(DefaultConfig(), units,
		storage.NewZFS(env("METAL_POOL", "metal"), env("METAL_KERNEL_DIR", "/var/lib/metal/kernels"), env("METAL_IMAGES_DIR", "/var/lib/metal/images")),
		network.NewLinux())
}

func spec(image, pub string) vm.Spec {
	return vm.Spec{
		VCPUs: 1, MemMiB: 256,
		Image:   vm.ImageRef{Name: image},
		Network: vm.NetworkRef{Name: "default"},
		SSHKeys: []string{pub},
	}
}

// sshCmd runs one command in the VM's netns over SSH, returning trimmed stdout.
func sshCmd(id, cmd string) (string, error) {
	out, err := exec.CommandContext(context.Background(), "ip", "netns", "exec", "metal-"+id,
		"ssh", "-i", os.Getenv("METAL_SSH_KEY"),
		"-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=3",
		env("METAL_SSH_USER", "root")+"@172.16.0.2", cmd).Output()
	return strings.TrimSpace(string(out)), err
}

// run runs a command over SSH and fails the test if it errors.
func run(t *testing.T, id, cmd string) string {
	t.Helper()
	out, err := sshCmd(id, cmd)
	if err != nil {
		t.Fatalf("ssh %q: %v", cmd, err)
	}
	return out
}

// waitSSH waits for the guest to accept SSH.
func waitSSH(t *testing.T, id string) bool {
	t.Helper()
	for range 30 {
		if out, err := sshCmd(id, "echo ok"); err == nil && out == "ok" {
			return true
		}
		time.Sleep(2 * time.Second)
	}
	return false
}

// bootVM creates a VM, starts it, waits for SSH, and registers cleanup.
func bootVM(t *testing.T, d *Driver, s vm.Spec) vm.VM {
	t.Helper()
	m, err := d.Create(context.Background(), s)
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	t.Cleanup(func() { _ = m.Destroy(context.Background()) })
	if err := m.Start(context.Background()); err != nil {
		t.Fatalf("start: %v", err)
	}
	if !waitSSH(t, m.ID()) {
		t.Fatalf("ssh into %s never succeeded", m.ID())
	}
	return m
}

func TestBootAndSSH(t *testing.T) {
	image, pub := skipUnlessHost(t)
	bootVM(t, newDriver(t), spec(image, pub))
}

// A memory snapshot restored in place must roll the disk back and resume the same
// RAM: the boot_id (generated once per boot, kept in RAM) is unchanged.
func TestWarmSnapshotRestore(t *testing.T) {
	image, pub := skipUnlessHost(t)
	ctx := context.Background()
	m := bootVM(t, newDriver(t), spec(image, pub))

	run(t, m.ID(), "echo BEFORE > /root/marker")
	bid0 := run(t, m.ID(), "cat /proc/sys/kernel/random/boot_id")

	if err := m.Snapshot(ctx, "demo", true); err != nil {
		t.Fatalf("snapshot: %v", err)
	}
	run(t, m.ID(), "echo AFTER > /root/marker")
	if err := m.RestoreSnapshot(ctx, "demo"); err != nil {
		t.Fatalf("restore: %v", err)
	}
	if !waitSSH(t, m.ID()) {
		t.Fatal("ssh after restore never succeeded")
	}

	if got := run(t, m.ID(), "cat /root/marker"); got != "BEFORE" {
		t.Errorf("marker = %q, want BEFORE (disk not rolled back)", got)
	}
	if got := run(t, m.ID(), "cat /proc/sys/kernel/random/boot_id"); got != bid0 {
		t.Errorf("boot_id = %q, want %q (cold booted, RAM not restored)", got, bid0)
	}
}

// A snapshot promoted to a warm image must produce an independent image: after the
// source VM is destroyed, a VM created from the image resumes the captured RAM and
// disk (same boot_id and marker).
func TestPromoteAndWarmCreate(t *testing.T) {
	image, pub := skipUnlessHost(t)
	ctx := context.Background()
	d := newDriver(t)
	src := bootVM(t, d, spec(image, pub))

	run(t, src.ID(), "echo BEFORE > /root/marker")
	bid0 := run(t, src.ID(), "cat /proc/sys/kernel/random/boot_id")
	if err := src.Snapshot(ctx, "demo", true); err != nil {
		t.Fatalf("snapshot: %v", err)
	}
	ref := "golden-" + src.ID()[:8]
	if err := src.Promote(ctx, "demo", ref); err != nil {
		t.Fatalf("promote: %v", err)
	}
	t.Cleanup(func() { _ = d.DeleteImage(context.Background(), ref) })
	if err := src.Destroy(ctx); err != nil {
		t.Fatalf("destroy source: %v", err)
	}

	clone := bootVM(t, d, spec(ref, pub))
	if got := run(t, clone.ID(), "cat /root/marker"); got != "BEFORE" {
		t.Errorf("clone marker = %q, want BEFORE (not from the captured disk)", got)
	}
	if got := run(t, clone.ID(), "cat /proc/sys/kernel/random/boot_id"); got != bid0 {
		t.Errorf("clone boot_id = %q, want %q (not from the captured RAM)", got, bid0)
	}
}
