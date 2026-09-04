package firecracker

import (
	"context"
	"fmt"
	"os"
	"strconv"
	"time"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/systemd"
	"github.com/frappe/atlas/metal/internal/vm"
)

const (
	networkInterfaceID     = "eth0"
	metadataServiceAddress = "169.254.169.254"
	metadataServiceVersion = "V2"
	rootDriveIdentifier    = "drive0"
	rootDrivePath          = "/rootfs.img"
)

func configure(
	operationContext context.Context,
	client *api.Client,
	virtualMachineID string,
	specification vm.Spec,
	bootConfiguration storage.BootConfiguration,
	networkInterface network.Interface,
) error {
	machineConfiguration := api.MachineConfig{
		VCPUCount:  specification.VCPUs,
		MemSizeMiB: specification.MemoryMiB,
	}
	if err := client.PutMachineConfig(operationContext, machineConfiguration); err != nil {
		return err
	}

	bootSource := api.BootSource{
		KernelImagePath: bootConfiguration.Kernel,
		BootArgs:        bootArguments(bootConfiguration, networkInterface),
	}
	if err := client.PutBootSource(operationContext, bootSource); err != nil {
		return err
	}

	for driveIndex, drive := range bootConfiguration.Drives {
		request := api.Drive{
			DriveID:      "drive" + strconv.Itoa(driveIndex),
			PathOnHost:   drive.Path,
			IsRootDevice: drive.Root,
			IsReadOnly:   drive.ReadOnly,
		}
		if err := client.PutDrive(operationContext, request); err != nil {
			return err
		}
	}

	interfaceRequest := api.NetworkInterface{
		IfaceID:     networkInterfaceID,
		HostDevName: networkInterface.TapName,
		GuestMAC:    networkInterface.MACAddress,
	}
	if err := client.PutNetworkInterface(operationContext, interfaceRequest); err != nil {
		return err
	}

	metadataConfiguration := api.MMDSConfig{
		NetworkInterfaces: []string{networkInterfaceID},
		Version:           metadataServiceVersion,
		IPv4Address:       metadataServiceAddress,
		IMDSCompat:        true,
	}
	if err := client.PutMMDSConfig(operationContext, metadataConfiguration); err != nil {
		return err
	}

	return client.PutMMDS(operationContext, metadataServiceData(
		virtualMachineID,
		networkInterface.GuestIPAddress,
		networkInterface.MACAddress,
		specification,
	))
}

func metadataServiceData(virtualMachineID, ipAddress, macAddress string, specification vm.Spec) map[string]any {
	publicKeys := make(map[string]any, len(specification.SSHKeys))
	for keyIndex, sshKey := range specification.SSHKeys {
		publicKeys[strconv.Itoa(keyIndex)] = map[string]any{"openssh-key": sshKey}
	}

	metadata := map[string]any{
		"instance-id": virtualMachineID,
		"public-keys": publicKeys,
	}
	if specification.Hostname != "" {
		metadata["local-hostname"] = specification.Hostname
	}
	if ipAddress != "" {
		metadata["local-ipv4"] = ipAddress
	}
	if macAddress != "" {
		metadata["mac"] = macAddress
	}
	if specification.Network.PublicIPv4 != "" {
		metadata["public-ipv4"] = specification.Network.PublicIPv4
	}

	data := map[string]any{"latest": map[string]any{"meta-data": metadata}}
	if specification.UserData != "" {
		data["latest"].(map[string]any)["user-data"] = specification.UserData
	}

	return data
}

func bootArguments(bootConfiguration storage.BootConfiguration, networkInterface network.Interface) string {
	networkArgument := fmt.Sprintf(
		"ip=%s::%s:255.255.255.0::eth0:off",
		networkInterface.GuestIPAddress,
		networkInterface.GatewayIPAddress,
	)
	return bootConfiguration.KernelArgs + " " + networkArgument
}

func resourceLimits(specification vm.Spec) systemd.Limits {
	// A memory snapshot needs space for guest memory and its memory file.
	return systemd.Limits{
		MemoryMaxBytes: (2*int64(specification.MemoryMiB) + 128) << 20,
		CPUQuotaPct:    specification.VCPUs * 100,
	}
}

func waitSocket(operationContext context.Context, path string) error {
	for {
		if _, err := os.Stat(path); err == nil {
			return nil
		}

		select {
		case <-operationContext.Done():
			return operationContext.Err()
		case <-time.After(50 * time.Millisecond):
		}
	}
}
