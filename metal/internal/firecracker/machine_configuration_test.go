package firecracker

import (
	"strings"
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

func TestImageCannotSupplyTheDiskEncryptionMode(t *testing.T) {
	for _, testCase := range []struct {
		name          string
		specification vm.Specification
	}{
		{"plain", vm.Specification{}},
		{"encrypted", vm.Specification{DiskEncryption: vm.DiskEncryptionLUKS2}},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			err := validateBootConfiguration(testCase.specification, storage.BootConfiguration{
				Initrd:     "/initrd",
				KernelArgs: "console=ttyS0 atlas.disk_encryption=luks2",
			})
			if err == nil || !strings.Contains(err.Error(), "boot arguments contain atlas.disk_encryption") {
				t.Fatalf("error = %v, want image-owned encryption mode rejection", err)
			}
		})
	}
}

func TestEncryptedBootSourceIncludesOnlyTheEncryptionMode(t *testing.T) {
	specification := vm.Specification{DiskEncryption: vm.DiskEncryptionLUKS2}
	configuration := storage.BootConfiguration{
		Kernel:     "/vmlinux",
		Initrd:     "/initrd",
		KernelArgs: "console=ttyS0",
	}
	networkInterface := network.Interface{
		GuestIPAddress:   "172.16.0.2",
		GatewayIPAddress: "172.16.0.1",
	}

	source := bootSource(specification, configuration, networkInterface)

	want := "console=ttyS0 ip=172.16.0.2::172.16.0.1:255.255.255.0::eth0:off atlas.disk_encryption=luks2"
	if source.BootArgs != want {
		t.Fatalf("boot arguments = %q, want %q", source.BootArgs, want)
	}
}

func TestEncryptedBootRequiresAnInitrd(t *testing.T) {
	for _, testCase := range []struct {
		name          string
		specification vm.Specification
		configuration storage.BootConfiguration
		wantError     bool
	}{
		{"plain", vm.Specification{}, storage.BootConfiguration{}, false},
		{"unknown mode", vm.Specification{DiskEncryption: "luks1"}, storage.BootConfiguration{}, true},
		{
			"missing initrd",
			vm.Specification{DiskEncryption: vm.DiskEncryptionLUKS2},
			storage.BootConfiguration{},
			true,
		},
		{
			"encrypted",
			vm.Specification{DiskEncryption: vm.DiskEncryptionLUKS2},
			storage.BootConfiguration{Initrd: "/initrd"},
			false,
		},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			err := validateBootConfiguration(testCase.specification, testCase.configuration)
			if (err != nil) != testCase.wantError {
				t.Fatalf("error = %v, want error = %v", err, testCase.wantError)
			}
		})
	}
}
