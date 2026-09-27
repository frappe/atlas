package datum

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadTokenBundleParsesHostAndVMs(t *testing.T) {
	path := filepath.Join(t.TempDir(), "datum-tokens.json")
	if err := os.WriteFile(path, []byte(`{"host":"host-token","vms":{"vm-1":"vm-1-token"}}`), 0o600); err != nil {
		t.Fatal(err)
	}

	bundle, err := ReadTokenBundle(path)
	if err != nil {
		t.Fatal(err)
	}
	if bundle.Host != "host-token" {
		t.Fatalf("Host = %q", bundle.Host)
	}
	if bundle.VMs["vm-1"] != "vm-1-token" {
		t.Fatalf("VMs[vm-1] = %q", bundle.VMs["vm-1"])
	}
}

func TestReadTokenBundleRejectsAMissingFile(t *testing.T) {
	if _, err := ReadTokenBundle(filepath.Join(t.TempDir(), "missing.json")); err == nil {
		t.Fatal("expected an error for a missing file")
	}
}
