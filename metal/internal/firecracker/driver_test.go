package firecracker

import (
	"testing"

	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

func TestBootArgs(t *testing.T) {
	boot := storage.BootConfig{KernelArgs: "console=ttyS0"}
	nic := network.NIC{GuestIP: "172.16.0.2", GatewayIP: "172.16.0.1"}
	got := bootArgs(boot, nic)
	want := "console=ttyS0 ip=172.16.0.2::172.16.0.1:255.255.255.0::eth0:off"
	if got != want {
		t.Errorf("bootArgs = %q", got)
	}
}

func TestMmdsData(t *testing.T) {
	d := mmdsData([]string{"ssh-ed25519 AAAA...", "ssh-rsa BBBB..."})
	pk := d["latest"].(map[string]any)["meta-data"].(map[string]any)["public-keys"].(map[string]any)
	if got := pk["0"].(map[string]any)["openssh-key"]; got != "ssh-ed25519 AAAA..." {
		t.Errorf("key 0 = %v", got)
	}
	if got := pk["1"].(map[string]any)["openssh-key"]; got != "ssh-rsa BBBB..." {
		t.Errorf("key 1 = %v", got)
	}
}

func TestLimits(t *testing.T) {
	l := limits(vm.Spec{VCPUs: 2, MemMiB: 512})
	// 2x the guest RAM plus 128 MiB headroom, so a memory snapshot (guest pages +
	// an equal-size mem file, both charged to the cgroup) does not OOM-kill the VM.
	if want := int64(2*512+128) << 20; l.MemoryMaxBytes != want {
		t.Errorf("mem = %d, want %d", l.MemoryMaxBytes, want)
	}
	if l.CPUQuotaPct != 200 {
		t.Errorf("cpu = %d", l.CPUQuotaPct)
	}
}
