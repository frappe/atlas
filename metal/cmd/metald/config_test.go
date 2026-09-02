package main

import (
	"os"
	"path/filepath"
	"testing"
)

// writeConfig writes a config.toml in a temp dir and returns its path.
func writeConfig(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.toml")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// A run with no file and no env keeps the built-in defaults.
func TestLoadDefaults(t *testing.T) {
	t.Chdir(t.TempDir()) // no config.toml in the working dir
	o, err := load("")
	if err != nil {
		t.Fatal(err)
	}
	if o != defaultOpts() {
		t.Fatalf("got %+v, want defaults %+v", o, defaultOpts())
	}
}

// A config.toml value overrides the default.
func TestLoadFileOverridesDefault(t *testing.T) {
	path := writeConfig(t, "listen = \"0.0.0.0:9000\"\n[storage]\npool = \"tank\"\n")
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

// An env var overrides both the file and the default.
func TestLoadEnvOverridesFile(t *testing.T) {
	path := writeConfig(t, "listen = \"0.0.0.0:9000\"\n")
	t.Setenv("METALD_LISTEN", "127.0.0.1:1234")
	o, err := load(path)
	if err != nil {
		t.Fatal(err)
	}
	if o.listen != "127.0.0.1:1234" {
		t.Errorf("listen = %q, want the env value", o.listen)
	}
}

// An explicit path that does not exist is an error; a missing default file is not.
func TestLoadMissingFile(t *testing.T) {
	if _, err := load("/no/such/config.toml"); err == nil {
		t.Error("explicit missing path: want error, got nil")
	}
	t.Chdir(t.TempDir())
	if _, err := load(""); err != nil {
		t.Errorf("missing default file: want no error, got %v", err)
	}
}
