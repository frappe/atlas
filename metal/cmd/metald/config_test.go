package main

import (
	"os"
	"path/filepath"
	"testing"
)

// writeConfig writes a configuration file in a temporary directory.
func writeConfig(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.toml")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadDefaults(t *testing.T) {
	o, err := load(writeConfig(t, ""))
	if err != nil {
		t.Fatal(err)
	}
	if o != defaultOpts() {
		t.Fatalf("got %+v, want defaults %+v", o, defaultOpts())
	}
}

func TestLoadFileOverridesDefault(t *testing.T) {
	path := writeConfig(t, "[metald]\nlisten = \"0.0.0.0:9000\"\n[zfs]\npool = \"tank\"\n")
	o, err := load(path)
	if err != nil {
		t.Fatal(err)
	}
	if o.listen != "0.0.0.0:9000" {
		t.Errorf("listen = %q, want the file value", o.listen)
	}
	if o.pool != "tank" {
		t.Errorf("pool = %q, want the file value", o.pool)
	}
	if o.kernelDir != defaultOpts().kernelDir {
		t.Errorf("kernelDir = %q, want the default for an unset key", o.kernelDir)
	}
}

// base_dir moves every directory metald derives from it.
func TestLoadBaseDirMovesDerivedDirs(t *testing.T) {
	path := writeConfig(t, "[metald]\nbase_dir = \"/srv/metal\"\n")
	o, err := load(path)
	if err != nil {
		t.Fatal(err)
	}
	if o.cfg.MachinesDir != "/srv/metal/machines" {
		t.Errorf("machinesDir = %q", o.cfg.MachinesDir)
	}
	if o.kernelDir != "/srv/metal/kernels" {
		t.Errorf("kernelDir = %q", o.kernelDir)
	}
}

func TestLoadMissingFile(t *testing.T) {
	if _, err := load("/no/such/config.toml"); err == nil {
		t.Error("explicit missing path: want error, got nil")
	}
}

// Startup creates every directory the config names, each with its own mode.
func TestMakeDirs(t *testing.T) {
	dir := t.TempDir()
	o := defaultOpts()
	o.cfg.MachinesDir = filepath.Join(dir, "machines")
	o.cfg.SocketsDir = filepath.Join(dir, "run")
	o.kernelDir = filepath.Join(dir, "kernels")

	if err := makeDirs(o); err != nil {
		t.Fatal(err)
	}
	if err := makeDirs(o); err != nil {
		t.Fatalf("makeDirs is not repeatable: %v", err)
	}
	for path, want := range map[string]os.FileMode{
		o.cfg.MachinesDir: 0o750,
		o.cfg.SocketsDir:  0o700,
		o.kernelDir:       0o755,
	} {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		if got := info.Mode().Perm(); got != want {
			t.Errorf("%s mode = %o, want %o", path, got, want)
		}
	}
}
