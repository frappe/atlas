package firecracker

import (
	"context"
	"fmt"
	"os"
	"time"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/network"
	platform "github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

const (
	networkInterfaceID     = "eth0"
	metadataServiceAddress = "169.254.169.254"
	metadataServiceVersion = "V2"
	rootDriveIdentifier    = "drive0"
	rootDrivePath          = "/rootfs.img"

	// guestNetworkMask is the guest network size. Every guest is alone in its
	// namespace, so one fixed mask serves them all.
	guestNetworkMask = "255.255.255.0"

	// memorySnapshotOverheadMiB covers Firecracker itself on top of guest memory.
	memorySnapshotOverheadMiB = 128

	// socketPollInterval is how often the jailed API socket is checked for.
	socketPollInterval = 50 * time.Millisecond
)

// configure applies the complete boot configuration to a started Firecracker
// process. The metadata service is configured before its data is written,
// because Firecracker rejects data for an unconfigured service.
func configure(
	operationContext context.Context,
	client *api.Client,
	virtualMachineID string,
	specification vm.Specification,
	bootConfiguration storage.BootConfiguration,
	networkInterface network.Interface,
) error {
	machineConfiguration := api.MachineConfig{
		VCPUCount:  specification.VirtualCPUCount(),
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
		request := driveRequest(driveIndex, drive, specification.Disk, len(bootConfiguration.Drives))
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

	return client.PutMMDS(operationContext, specification.MetadataServiceData(
		virtualMachineID,
		networkInterface.GuestIPAddress,
		networkInterface.MACAddress,
	))
}

// bootArguments appends the guest network to the image kernel arguments, so the
// guest is addressable before any userspace network configuration runs.
func bootArguments(bootConfiguration storage.BootConfiguration, networkInterface network.Interface) string {
	networkArgument := fmt.Sprintf(
		"ip=%s::%s:"+guestNetworkMask+"::eth0:off",
		networkInterface.GuestIPAddress,
		networkInterface.GatewayIPAddress,
	)
	return bootConfiguration.KernelArgs + " " + networkArgument
}

// resourceLimits caps the unit. Memory is twice the guest size plus overhead,
// because a memory snapshot holds guest memory and its memory file at once.
func resourceLimits(specification vm.Specification) platform.Limits {
	return platform.Limits{
		MemoryMaxBytes: (2*int64(specification.MemoryMiB) + memorySnapshotOverheadMiB) << 20,
		CPUMillicores:  specification.CPUMillicores,
	}
}

// waitSocket waits for Firecracker to create its API socket. The process is
// started by systemd, so there is nothing to wait on except the socket.
func waitSocket(operationContext context.Context, path string) error {
	for {
		if _, err := os.Stat(path); err == nil {
			return nil
		}

		select {
		case <-operationContext.Done():
			return operationContext.Err()
		case <-time.After(socketPollInterval):
		}
	}
}
