package platform

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

const cgroupRoot = "/sys/fs/cgroup"

// Usage describes a unit's current cgroup resource use.
type Usage struct {
	// MemoryBytes is a gauge, not cumulative.
	MemoryBytes uint64
	// CPUUsageMicroseconds is cumulative since the cgroup was created.
	CPUUsageMicroseconds uint64
	DiskReadBytes        uint64
	DiskWriteBytes       uint64
	DiskReadOperations   uint64
	DiskWriteOperations  uint64
}

// controlGroup is the path reported by systemd's ControlGroup property.
func readUsage(controlGroup string) (Usage, error) {
	memoryBytes, err := readMemoryCurrent(cgroupFile(controlGroup, "memory.current"))
	if err != nil {
		return Usage{}, err
	}
	cpuMicroseconds, err := readCPUUsageMicroseconds(cgroupFile(controlGroup, "cpu.stat"))
	if err != nil {
		return Usage{}, err
	}
	return Usage{MemoryBytes: memoryBytes, CPUUsageMicroseconds: cpuMicroseconds}, nil
}

func readDiskUsage(controlGroup, device string) (Usage, error) {
	path := cgroupFile(controlGroup, "io.stat")
	data, err := os.ReadFile(path)
	if err != nil {
		return Usage{}, fmt.Errorf("read %s: %w", path, err)
	}
	return parseDiskUsage(data, device)
}

func parseDiskUsage(data []byte, device string) (Usage, error) {
	for _, line := range bytes.Split(data, []byte{'\n'}) {
		fields := bytes.Fields(line)
		if len(fields) == 0 || string(fields[0]) != device {
			continue
		}
		var usage Usage
		for _, field := range fields[1:] {
			key, value, found := bytes.Cut(field, []byte{'='})
			if !found {
				return Usage{}, fmt.Errorf("malformed io.stat field %q", field)
			}
			count, err := strconv.ParseUint(string(value), 10, 64)
			if err != nil {
				return Usage{}, fmt.Errorf("parse io.stat %s: %w", key, err)
			}
			switch string(key) {
			case "rbytes":
				usage.DiskReadBytes = count
			case "wbytes":
				usage.DiskWriteBytes = count
			case "rios":
				usage.DiskReadOperations = count
			case "wios":
				usage.DiskWriteOperations = count
			}
		}
		return usage, nil
	}
	return Usage{}, nil
}

func cgroupFile(controlGroup, name string) string {
	return filepath.Join(cgroupRoot, controlGroup, name)
}

// readMemoryCurrent reads a cgroup v2 memory.current file. A cgroup that has
// already been removed reads as zero, the same as an absent systemd unit.
func readMemoryCurrent(path string) (uint64, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return 0, nil
	}
	if err != nil {
		return 0, fmt.Errorf("read %s: %w", path, err)
	}
	return parseMemoryCurrent(data)
}

func parseMemoryCurrent(data []byte) (uint64, error) {
	value, err := strconv.ParseUint(strings.TrimSpace(string(data)), 10, 64)
	if err != nil {
		return 0, fmt.Errorf("parse memory.current: %w", err)
	}
	return value, nil
}

// readCPUUsageMicroseconds reads a cgroup v2 cpu.stat file's usage_usec
// field. A cgroup that has already been removed reads as zero, the same as
// an absent systemd unit.
func readCPUUsageMicroseconds(path string) (uint64, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return 0, nil
	}
	if err != nil {
		return 0, fmt.Errorf("read %s: %w", path, err)
	}
	return parseCPUUsageMicroseconds(data)
}

// parseCPUUsageMicroseconds parses cpu.stat's "key value" lines and returns
// usage_usec, the cumulative CPU time the cgroup has consumed.
func parseCPUUsageMicroseconds(data []byte) (uint64, error) {
	scanner := bufio.NewScanner(bytes.NewReader(data))
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) != 2 || fields[0] != "usage_usec" {
			continue
		}
		value, err := strconv.ParseUint(fields[1], 10, 64)
		if err != nil {
			return 0, fmt.Errorf("parse cpu.stat usage_usec: %w", err)
		}
		return value, nil
	}
	if err := scanner.Err(); err != nil {
		return 0, fmt.Errorf("read cpu.stat: %w", err)
	}
	return 0, fmt.Errorf("cpu.stat has no usage_usec field")
}
