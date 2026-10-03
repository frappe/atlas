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
