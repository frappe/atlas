package firecracker

import (
	"testing"

	"github.com/frappe/metal/internal/network"
	"github.com/frappe/metal/internal/storage"
	"github.com/frappe/metal/internal/vm"
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
	if l.MemoryMaxBytes != 512<<20 {
		t.Errorf("mem = %d", l.MemoryMaxBytes)
	}
	if l.CPUQuotaPct != 200 {
		t.Errorf("cpu = %d", l.CPUQuotaPct)
	}
}
