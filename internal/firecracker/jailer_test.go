package firecracker

import (
	"os"
	"slices"
	"strings"
	"testing"
)

func testConfig(dir string) Config {
	c := DefaultConfig()
	c.ChrootBase = dir + "/chroot"
	c.VarDir = dir + "/vms"
	return c
}

func TestLayout(t *testing.T) {
	c := DefaultConfig()
	if got := c.chrootRoot("abc"); got != "/srv/jailer/firecracker/abc/root" {
		t.Errorf("chrootRoot = %q", got)
	}
	if got := c.sockPath("abc"); got != "/srv/jailer/firecracker/abc/root/run/firecracker.socket" {
		t.Errorf("sockPath = %q", got)
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
		"--chroot-base-dir", "/srv/jailer",
		"--netns", "/run/netns/metal-abc",
		"--",
		"--api-sock", "run/firecracker.socket",
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
	if got != vc {
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

func TestNewIDDistinct(t *testing.T) {
	a, b := newID(), newID()
	if a == b {
		t.Error("ids not distinct")
	}
	if len(a) != 16 {
		t.Errorf("id length = %d", len(a))
	}
}
