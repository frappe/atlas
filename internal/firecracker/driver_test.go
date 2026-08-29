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

func TestLimits(t *testing.T) {
	l := limits(vm.Spec{VCPUs: 2, MemMiB: 512})
	if l.MemoryMaxBytes != 512<<20 {
		t.Errorf("mem = %d", l.MemoryMaxBytes)
	}
	if l.CPUQuotaPct != 200 {
		t.Errorf("cpu = %d", l.CPUQuotaPct)
	}
}
