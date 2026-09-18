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
	options, err := load(writeConfig(t, ""))
	if err != nil {
		t.Fatal(err)
	}
	if options != defaultOptions() {
		t.Fatalf("got %+v, want defaults %+v", options, defaultOptions())
	}
}

func TestLoadFileOverridesDefault(t *testing.T) {
	path := writeConfig(t, "[metald]\nlisten = \"0.0.0.0:9000\"\n[zfs]\npool = \"tank\"\n")
	options, err := load(path)
	if err != nil {
		t.Fatal(err)
	}
	if options.listen != "0.0.0.0:9000" {
		t.Errorf("listen = %q, want the file value", options.listen)
	}
	if options.pool != "tank" {
		t.Errorf("pool = %q, want the file value", options.pool)
	}
	if options.imagesDir != defaultOptions().imagesDir {
		t.Errorf("imagesDir = %q, want the default for an unset key", options.imagesDir)
	}
}

func TestLoadFeatureSwitches(t *testing.T) {
	path := writeConfig(t, "[wg_mesh]\nenabled = false\n[traffic_monitor]\nenabled = false\n")
	options, err := load(path)
	if err != nil {
		t.Fatal(err)
	}
	if options.mesh.enabled || options.trafficMonitor.enabled {
		t.Fatalf("feature switches = mesh %t, traffic monitor %t, want both disabled", options.mesh.enabled, options.trafficMonitor.enabled)
	}
}

func TestLoadBaseDirMovesDerivedDirs(t *testing.T) {
	path := writeConfig(t, "[metald]\nbase_dir = \"/srv/metal\"\n")
	options, err := load(path)
	if err != nil {
		t.Fatal(err)
	}
	if options.cfg.MachinesDir != "/srv/metal/machines" {
		t.Errorf("machinesDir = %q", options.cfg.MachinesDir)
	}
	if options.imagesDir != "/srv/metal/images" {
		t.Errorf("imagesDir = %q", options.imagesDir)
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

func TestLoadMigrationFinalDelta(t *testing.T) {
	if got := defaultOptions().migration.finalDeltaMiB; got != 512 {
		t.Fatalf("default final_delta_mib = %d, want 512", got)
	}
	path := writeConfig(t, "[migration]\nfinal_delta_mib = 256\n")
	options, err := load(path)
	if err != nil {
		t.Fatal(err)
	}
	if options.migration.finalDeltaMiB != 256 {
		t.Errorf("final_delta_mib = %d, want the file value", options.migration.finalDeltaMiB)
	}
}

func TestLoadMigrationTransferPort(t *testing.T) {
	if got := defaultOptions().migration.transferPort; got != 9001 {
		t.Fatalf("default transfer_port = %d, want 9001", got)
	}
	path := writeConfig(t, "[migration]\ntransfer_port = 9100\n")
	options, err := load(path)
	if err != nil {
		t.Fatal(err)
	}
	if options.migration.transferPort != 9100 {
		t.Errorf("transfer_port = %d, want the file value", options.migration.transferPort)
	}
}

func TestLoadMissingFile(t *testing.T) {
	if _, err := load("/no/such/config.toml"); err == nil {
		t.Error("explicit missing path: want error, got nil")
	}
}

func TestUnicastPeersFilePath(t *testing.T) {
	defaults := defaultOptions()
	if got := defaults.unicastPeersFilePath(); got != "/var/lib/metal/unicast-peers" {
		t.Fatalf("default unicast peers file = %q", got)
	}

	moved := defaultOptions()
	moved.baseDir = "/srv/metal"
	if got := moved.unicastPeersFilePath(); got != "/srv/metal/unicast-peers" {
		t.Fatalf("base_dir unicast peers file = %q", got)
	}

	configured := writeConfig(t, "[wg_mesh]\npeers_file = \"/etc/atlas/unicast-peers\"\n")
	options, err := load(configured)
	if err != nil {
		t.Fatal(err)
	}
	if got := options.unicastPeersFilePath(); got != "/etc/atlas/unicast-peers" {
		t.Fatalf("configured unicast peers file = %q", got)
	}
}

func TestMakeDirs(t *testing.T) {
	dir := t.TempDir()
	options := defaultOptions()
	options.baseDir = dir
	options.deriveDirs()
	options.cfg.SocketsDir = filepath.Join(dir, "run")

	if err := makeDirs(options); err != nil {
		t.Fatal(err)
	}
	if err := makeDirs(options); err != nil {
		t.Fatalf("makeDirs is not repeatable: %v", err)
	}
	for path, want := range map[string]os.FileMode{
		options.cfg.MachinesDir: 0o750,
		options.cfg.SocketsDir:  0o700,
		options.imagesDir:       0o755,
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
