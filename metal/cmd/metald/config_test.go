package main

import (
	"os"
	"path/filepath"
	"testing"
)

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
	if o.imagesDir != defaultOpts().imagesDir {
		t.Errorf("imagesDir = %q, want the default for an unset key", o.imagesDir)
	}
}

func TestLoadBaseDirMovesDerivedDirs(t *testing.T) {
	path := writeConfig(t, "[metald]\nbase_dir = \"/srv/metal\"\n")
	o, err := load(path)
	if err != nil {
		t.Fatal(err)
	}
	if o.cfg.MachinesDir != "/srv/metal/machines" {
		t.Errorf("machinesDir = %q", o.cfg.MachinesDir)
	}
	if o.imagesDir != "/srv/metal/images" {
		t.Errorf("imagesDir = %q", o.imagesDir)
	}
}

func TestLoadAuthenticationTokenHash(t *testing.T) {
	const tokenHash = "4c5dc9b7708905f77f5e5d16316b5dfb425e68cb326dcd55a860e90a7707031e"
	path := writeConfig(t, "[metald]\nauth_token_hash = \""+tokenHash+"\"\n")
	options, err := load(path)
	if err != nil {
		t.Fatal(err)
	}
	if options.authTokenHash != tokenHash {
		t.Errorf("authTokenHash = %q, want configured hash", options.authTokenHash)
	}
}

func TestLoadMissingFile(t *testing.T) {
	if _, err := load("/no/such/config.toml"); err == nil {
		t.Error("explicit missing path: want error, got nil")
	}
}

func TestMakeDirs(t *testing.T) {
	dir := t.TempDir()
	o := defaultOpts()
	o.baseDir = dir
	o.deriveDirs()
	o.cfg.SocketsDir = filepath.Join(dir, "run")

	if err := makeDirs(o); err != nil {
		t.Fatal(err)
	}
	if err := makeDirs(o); err != nil {
		t.Fatalf("makeDirs is not repeatable: %v", err)
	}
	for path, want := range map[string]os.FileMode{
		o.cfg.MachinesDir: 0o750,
		o.cfg.SocketsDir:  0o700,
		o.imagesDir:       0o755,
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
