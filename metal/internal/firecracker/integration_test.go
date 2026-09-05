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
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/frappe/atlas/metal/internal/console"
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
	if image == "" || pubPath == "" || os.Getenv("METAL_SSH_KEY") == "" ||
		os.Getenv("METAL_IMAGE_URL") == "" || os.Getenv("METAL_IMAGE_SHA256") == "" ||
		os.Getenv("METAL_KERNEL_URL") == "" || os.Getenv("METAL_KERNEL_SHA256") == "" {
		t.Skip("set the METAL image, kernel, and SSH environment variables")
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
	stores := storage.NewStores(env("METAL_POOL", "metal"), env("METAL_IMAGES_DIR", "/var/lib/metal/images"))
	consoleBroker := console.NewBroker(t.TempDir())
	t.Cleanup(consoleBroker.Shutdown)
	return New(
		DefaultConfig(),
		units,
		stores.VirtualMachines,
		stores.Images,
		stores.Snapshots,
		network.NewLinuxAllocator(nil),
		consoleBroker,
	)
}

func spec(image, pub string) vm.Spec {
	return vm.Spec{
		VCPUs: 1, MemoryMiB: 256, DiskMiB: 1024,
		Image: vm.ImageRef{
			Name:         image,
			RootfsURL:    os.Getenv("METAL_IMAGE_URL"),
			RootfsSHA256: os.Getenv("METAL_IMAGE_SHA256"),
			KernelURL:    os.Getenv("METAL_KERNEL_URL"),
			KernelSHA256: os.Getenv("METAL_KERNEL_SHA256"),
			Architecture: runtime.GOARCH,
		},
		Network: vm.Network{Egress: vm.EgressUplink},
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
	m, err := d.Create(context.Background(), uuid.NewString(), s)
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
