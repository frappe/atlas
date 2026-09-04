package storage

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/frappe/atlas/metal/internal/hostcmd"
)

const defaultKernelArguments = "console=ttyS0 reboot=k panic=1 pci=off"

func createBlockDevice(sourceDevice, destinationPath string, userID, groupID uint32) error {
	deviceInformation, err := waitForBlockDevice(sourceDevice)
	if err != nil {
		return err
	}

	_ = os.Remove(destinationPath)
	if err := syscall.Mknod(destinationPath, syscall.S_IFBLK|0o600, int(deviceInformation.Rdev)); err != nil {
		return fmt.Errorf("create block device %s: %w", destinationPath, err)
	}
	if err := os.Chmod(destinationPath, 0o600); err != nil {
		return err
	}

	return os.Chown(destinationPath, int(userID), int(groupID))
}

func waitForBlockDevice(path string) (syscall.Stat_t, error) {
	var deviceInformation syscall.Stat_t
	for range 60 {
		if err := syscall.Stat(path, &deviceInformation); err == nil {
			return deviceInformation, nil
		}
		time.Sleep(50 * time.Millisecond)
	}

	return deviceInformation, fmt.Errorf("device %s did not appear", path)
}

func replaceHardLink(source, destination string) error {
	_ = os.Remove(destination)
	return os.Link(source, destination)
}

// LinkOrCopy shares data when possible and copies it when required.
func LinkOrCopy(ctx context.Context, source, destination string) error {
	_ = os.Remove(destination)
	if err := os.Link(source, destination); err == nil {
		return nil
	}

	return hostcmd.Run(ctx, "cp", "--reflink=auto", source, destination)
}

func kernelArguments(imageDirectory string) string {
	arguments, err := os.ReadFile(filepath.Join(imageDirectory, "boot-args"))
	if err != nil {
		return defaultKernelArguments
	}

	return strings.TrimSpace(string(arguments))
}
