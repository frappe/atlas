package storage

import (
	"context"
	"strconv"
	"strings"

	"github.com/frappe/atlas/metal/internal/hostcmd"
)

// DiskUsage reports disk size and allocated storage.
func (store *VirtualMachineStore) DiskUsage(ctx context.Context, virtualMachineID string) (Usage, error) {
	output, err := hostcmd.Output(
		ctx,
		"zfs",
		"get",
		"-Hp",
		"-o",
		"property,value",
		"volsize,used",
		store.pool.virtualMachineDataset(virtualMachineID),
	)
	if err != nil {
		return Usage{}, notFoundAware(err)
	}
	return parseDiskUsage(output), nil
}

func parseDiskUsage(output string) Usage {
	var usage Usage
	for _, line := range strings.Split(strings.TrimSpace(output), "\n") {
		fields := strings.Fields(line)
		if len(fields) != 2 {
			continue
		}

		valueBytes, _ := strconv.ParseInt(fields[1], 10, 64)
		switch fields[0] {
		case "volsize":
			usage.SizeMiB = int(valueBytes >> 20)
		case "used":
			usage.UsedMiB = int(valueBytes >> 20)
		}
	}
	return usage
}
