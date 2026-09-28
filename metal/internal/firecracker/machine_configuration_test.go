package firecracker

import (
	"testing"

	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

func TestBootSourceIncludesAnOptionalInitrd(t *testing.T) {
	networkInterface := network.Interface{
		GuestIPAddress:   "172.16.0.2",
		GatewayIPAddress: "172.16.0.1",
	}
	for _, testCase := range []struct {
		name   string
		initrd string
	}{
		{"plain", ""},
		{"initrd", "/initrd"},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			source := bootSource(vm.Specification{}, storage.BootConfiguration{
				Kernel:     "/vmlinux",
				Initrd:     testCase.initrd,
				KernelArgs: "console=ttyS0",
			}, networkInterface)

			if source.KernelImagePath != "/vmlinux" || source.InitrdPath != testCase.initrd {
				t.Fatalf("boot source = %+v", source)
			}
			if source.BootArgs != "console=ttyS0 ip=172.16.0.2::172.16.0.1:255.255.255.0::eth0:off" {
				t.Fatalf("boot arguments = %q", source.BootArgs)
			}
		})
	}
}
