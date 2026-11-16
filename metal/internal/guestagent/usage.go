package guestagent

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
)

// Usage is the resource use that the guest reports about itself.
type Usage struct {
	LoadAverage    [3]float64 `json:"load_average"`
	Memory         Memory     `json:"memory"`
	RootFilesystem Filesystem `json:"root_filesystem"`
}

// Memory is the guest memory use from /proc/meminfo.
type Memory struct {
	TotalBytes     uint64 `json:"total_bytes"`
	FreeBytes      uint64 `json:"free_bytes"`
	AvailableBytes uint64 `json:"available_bytes"`
	CachedBytes    uint64 `json:"cached_bytes"`
	SwapTotalBytes uint64 `json:"swap_total_bytes"`
	SwapFreeBytes  uint64 `json:"swap_free_bytes"`
}

// Filesystem is the size and use of one guest file system.
type Filesystem struct {
	SizeBytes   uint64 `json:"size_bytes"`
	UsedBytes   uint64 `json:"used_bytes"`
	InodesTotal uint64 `json:"inodes_total"`
	InodesUsed  uint64 `json:"inodes_used"`
}

// ReadUsage reads load and memory from procRoot and the use of the root file system.
func ReadUsage(procRoot string) (Usage, error) {
	var usage Usage
	var err error
	if usage.LoadAverage, err = readLoadAverage(filepath.Join(procRoot, "loadavg")); err != nil {
		return Usage{}, err
	}
	if usage.Memory, err = readMemory(filepath.Join(procRoot, "meminfo")); err != nil {
		return Usage{}, err
	}

	var stat unix.Statfs_t
	if err := unix.Statfs("/", &stat); err != nil {
		return Usage{}, fmt.Errorf("statfs /: %w", err)
	}
	blockBytes := uint64(stat.Frsize)
	usage.RootFilesystem = Filesystem{
		SizeBytes:   stat.Blocks * blockBytes,
		UsedBytes:   (stat.Blocks - stat.Bfree) * blockBytes,
		InodesTotal: stat.Files,
		InodesUsed:  stat.Files - stat.Ffree,
	}
	return usage, nil
}

func readLoadAverage(path string) ([3]float64, error) {
	var load [3]float64
	data, err := os.ReadFile(path)
	if err != nil {
		return load, err
	}
	if _, err := fmt.Sscan(string(data), &load[0], &load[1], &load[2]); err != nil {
		return load, fmt.Errorf("parse %s: %w", path, err)
	}
	return load, nil
}

func readMemory(path string) (Memory, error) {
	file, err := os.Open(path)
	if err != nil {
		return Memory{}, err
	}
	defer file.Close()

	var memory Memory
	fields := map[string]*uint64{
		"MemTotal": &memory.TotalBytes, "MemFree": &memory.FreeBytes, "MemAvailable": &memory.AvailableBytes,
		"Cached": &memory.CachedBytes, "SwapTotal": &memory.SwapTotalBytes, "SwapFree": &memory.SwapFreeBytes,
	}
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		name, value, _ := strings.Cut(scanner.Text(), ":")
		target, found := fields[name]
		if !found {
			continue
		}
		kibibytes, err := strconv.ParseUint(strings.TrimSuffix(strings.TrimSpace(value), " kB"), 10, 64)
		if err != nil {
			return Memory{}, fmt.Errorf("parse %s %s: %w", path, name, err)
		}
		*target = kibibytes << 10
	}
	return memory, scanner.Err()
}
