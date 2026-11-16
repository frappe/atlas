package firecracker

import (
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/google/uuid"
)

func testConfig(dir string) Config {
	c := DefaultConfig()
	c.MachinesDir = dir + "/machines"
	c.SocketsDir = dir + "/run"
	return c
}

func TestLayout(t *testing.T) {
	c := DefaultConfig()
	if got := c.chrootRoot("abc"); got != "/var/lib/metal/machines/abc/firecracker/abc/root" {
		t.Errorf("chrootRoot = %q", got)
	}
	want := "/var/lib/metal/machines/abc/firecracker/abc/root/run/firecracker.socket"
	if got := c.chrootSocketPath("abc"); got != want {
		t.Errorf("chrootSocketPath = %q", got)
	}
	if !strings.HasPrefix(c.chrootRoot("abc"), c.vmDir("abc")+"/") {
		t.Error("the chroot is outside the VM directory")
	}
}

func TestSockPathFitsSunPath(t *testing.T) {
	id := uuid.Must(uuid.NewV7()).String()
	for _, path := range []string{DefaultConfig().socketPath(id), DefaultConfig().vsockSocketPath(id)} {
		if len(path) > 108 {
			t.Errorf("%s is %d bytes, over the 108 byte limit", path, len(path))
		}
	}
	if len(DefaultConfig().chrootSocketPath(id)) <= 108 {
		t.Log("the chroot path fits today, but the link is what keeps it safe")
	}
}

func TestLinkSockets(t *testing.T) {
	c := testConfig(t.TempDir())
	if err := c.linkSockets("abc"); err != nil {
		t.Fatal(err)
	}
	if err := c.linkSockets("abc"); err != nil {
		t.Fatalf("linkSockets is not repeatable: %v", err)
	}
	got, err := os.Readlink(c.socketPath("abc"))
	if err != nil {
		t.Fatal(err)
	}
	if got != c.chrootSocketPath("abc") {
		t.Errorf("link points at %q", got)
	}
}

func TestJailerArgs(t *testing.T) {
	c := DefaultConfig()
	args := c.jailerArgs("abc", 100000, 100000, "/run/netns/metal-abc")
	want := []string{
		"--id", "abc",
		"--exec-file", "/usr/bin/firecracker",
		"--uid", "100000",
		"--gid", "100000",
		"--chroot-base-dir", "/var/lib/metal/machines/abc",
		"--netns", "/run/netns/metal-abc",
		"--",
		"--api-sock", "run/firecracker.socket",
		"--log-path", "firecracker.log",
		"--level", "Warn",
		"--mmds-size-limit", "262144",
		"--http-api-max-payload-size", "262144",
	}
	if !slices.Equal(args, want) {
		t.Errorf("args = %v", args)
	}
	for _, a := range args {
		if strings.ContainsRune(a, ' ') {
			t.Errorf("arg %q contains a space; systemd word-splitting would break", a)
		}
	}
}

func TestJailerEnv(t *testing.T) {
	c := testConfig(t.TempDir())
	if err := c.writeJailerEnv("abc", c.jailerArgs("abc", 1, 1, "/run/netns/metal-abc")); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(c.vmDir("abc") + "/jailer.env")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(string(b), "JAILER_ARGS=--id abc ") {
		t.Errorf("env = %q", b)
	}
}
