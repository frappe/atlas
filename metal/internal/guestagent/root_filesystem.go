package guestagent

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"slices"
	"strings"
	"time"
)

// growTimeout ends the grow before metald stops waiting after 30 seconds.
const growTimeout = 25 * time.Second

// GrowRootFilesystem grows the root file system to fill its disk. It does
// nothing when the file system already fills the disk.
func GrowRootFilesystem(ctx context.Context) error {
	device, filesystemType, err := rootDevice("/proc/self/mountinfo")
	if err != nil {
		return err
	}

	ctx, cancel := context.WithTimeout(ctx, growTimeout)
	defer cancel()
	switch filesystemType {
	case "ext2", "ext3", "ext4":
		return runCommand(ctx, "resize2fs", device)
	case "xfs":
		return runCommand(ctx, "xfs_growfs", "/")
	default:
		return fmt.Errorf("cannot grow a %s root file system", filesystemType)
	}
}

// rootDevice returns the device and type of the root mount. The source can be
// /dev/root, which has no device file, so it falls back to /dev/block/<major>:<minor>.
func rootDevice(path string) (string, string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", "", err
	}
	defer file.Close()

	var device, filesystemType string
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		// A line is "<id> <parent> <major:minor> <root> <mount point> <options> [fields] - <type> <source> <options>".
		fields := strings.Fields(scanner.Text())
		separator := slices.Index(fields, "-")
		if separator < 5 || len(fields) < separator+3 || fields[4] != "/" {
			continue
		}
		// A later root mount covers an earlier one, so the last line wins.
		filesystemType, device = fields[separator+1], fields[separator+2]
		if _, err := os.Stat(device); err != nil {
			device = "/dev/block/" + fields[2]
		}
	}
	if err := scanner.Err(); err != nil {
		return "", "", err
	}
	if device == "" {
		return "", "", fmt.Errorf("no root mount in %s", path)
	}
	return device, filesystemType, nil
}
