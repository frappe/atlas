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
	if got := defaultOptions().migration.transferPort; got != 9002 {
		t.Fatalf("default transfer_port = %d, want 9002", got)
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

func TestLoadTLSAndCoordination(t *testing.T) {
	path := writeConfig(t, `[metald]
coordination_listen = "[fdab::12]:9001"
[tls]
ca_file = "/tls/ca.crt"
certificate_file = "/tls/node.crt"
private_key_file = "/tls/node.key"
atlas_common_name = "atlas.example.test"
`)
	options, err := load(path)
	if err != nil {
		t.Fatal(err)
	}
	want := tlsOptions{
		caFile:          "/tls/ca.crt",
		certificateFile: "/tls/node.crt",
		privateKeyFile:  "/tls/node.key",
		atlasCommonName: "atlas.example.test",
	}
	if options.tls != want {
		t.Fatalf("TLS options = %+v", options.tls)
	}
	if options.coordinationListen != "[fdab::12]:9001" {
		t.Fatalf("coordination listen = %q", options.coordinationListen)
	}
}

func TestLoadMissingFile(t *testing.T) {
	if _, err := load("/no/such/config.toml"); err == nil {
		t.Error("explicit missing path: want error, got nil")
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

func TestTransferListenAddressNeedsANodeAddress(t *testing.T) {
	address, err := transferListenAddress("[fdab::12]:9001", 9002)
	if err != nil {
		t.Fatal(err)
	}
	if address != "[fdab::12]:9002" {
		t.Fatalf("transfer address = %q", address)
	}
	for _, coordination := range []string{"0.0.0.0:9001", "[::]:9001", "unix:/run/metal.sock"} {
		if _, err := transferListenAddress(coordination, 9002); err == nil {
			t.Fatalf("coordination address %q was accepted", coordination)
		}
	}
}
