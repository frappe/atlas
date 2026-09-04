package firecracker

import (
	"os"
	"reflect"
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
	if got := c.chrootSockPath("abc"); got != want {
		t.Errorf("chrootSockPath = %q", got)
	}
	if !strings.HasPrefix(c.chrootRoot("abc"), c.vmDir("abc")+"/") {
		t.Error("the chroot is outside the VM dir, so Destroy would leave it behind")
	}
}

func TestSockPathFitsSunPath(t *testing.T) {
	id := uuid.Must(uuid.NewV7()).String()
	if got := len(DefaultConfig().sockPath(id)); got > 108 {
		t.Errorf("sockPath is %d bytes, over the 108 byte limit", got)
	}
	if len(DefaultConfig().chrootSockPath(id)) <= 108 {
		t.Log("the chroot path fits today, but the link is what keeps it safe")
	}
}

func TestLinkSocket(t *testing.T) {
	c := testConfig(t.TempDir())
	if err := c.linkSocket("abc"); err != nil {
		t.Fatal(err)
	}
	if err := c.linkSocket("abc"); err != nil {
		t.Fatalf("linkSocket is not repeatable: %v", err)
	}
	got, err := os.Readlink(c.sockPath("abc"))
	if err != nil {
		t.Fatal(err)
	}
	if got != c.chrootSockPath("abc") {
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

func TestVMConfigRoundtrip(t *testing.T) {
	c := testConfig(t.TempDir())
	vc := vmConfig{ID: "abc", UID: 100000, GID: 100000, IP: "172.16.0.2", MAC: "02:aa:bb:cc:dd:ee", Sock: "/s"}
	if err := c.writeVMConfig(vc); err != nil {
		t.Fatal(err)
	}
	got, err := c.readVMConfig("abc")
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, vc) {
		t.Errorf("got %+v, want %+v", got, vc)
	}

	used, err := c.usedIDs()
	if err != nil {
		t.Fatal(err)
	}
	if !used[100000] {
		t.Errorf("usedIDs missing 100000: %v", used)
	}
}

func TestReadVMConfigNotFound(t *testing.T) {
	c := testConfig(t.TempDir())
	if _, err := c.readVMConfig("missing"); err == nil {
		t.Error("want error for missing config")
	}
}
