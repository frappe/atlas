//go:build integration

package firecracker

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/vm"
)

func TestSnapshotRestoresGatewayMACOnAnExistingTAP(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("needs root to create a network namespace")
	}
	ctx := t.Context()
	namespace := fmt.Sprintf("atlas-test-snapshot-tap-%d", os.Getpid())
	if err := platform.Run(ctx, "ip", "netns", "add", namespace); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = platform.Run(context.Background(), "ip", "netns", "del", namespace) })
	for _, arguments := range [][]string{
		{"tuntap", "add", "tap0", "mode", "tap"},
		{"link", "set", "dev", "tap0", "address", "02:aa:bb:cc:dd:ee", "up"},
	} {
		if _, err := platform.RunInNetworkNamespace(ctx, namespace, "ip", arguments...); err != nil {
			t.Fatal(err)
		}
	}
	networkInterface := vm.NetworkInterface{NetworkNamespacePath: filepath.Join("/run/netns", namespace), TapName: "tap0"}
	snapshot := savedState{Metadata: savedStateMetadata{TapMACAddress: "02:11:22:33:44:55"}}
	for range 2 {
		if err := snapshot.restoreNetwork(ctx, networkInterface); err != nil {
			t.Fatal(err)
		}
		address, err := platform.RunInNetworkNamespace(ctx, namespace, "cat", "/sys/class/net/tap0/address")
		if err != nil || strings.TrimSpace(address) != snapshot.Metadata.TapMACAddress {
			t.Fatalf("restored gateway MAC = %q, %v", address, err)
		}
	}
}
