package firecracker

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"runtime"
	"strings"

	"golang.org/x/sys/unix"

	"github.com/frappe/atlas/metal/internal/vm"
)

type migrationSnapshotCompatibility struct {
	Host string `json:"host"`
	vm.MemorySnapshotConfiguration
}

// RestoreMigrationSnapshot validates transferred state and loads it paused into a new jail.
func (runtime *Runtime) RestoreMigrationSnapshot(ctx context.Context, input vm.RuntimeMachine, directory string) error {
	compatibility, err := newMigrationSnapshotCompatibility(ctx, runtime.configuration.FirecrackerBin, input.Specification)
	if err != nil {
		return err
	}
	machine := runtime.newMachine(input)
	requirement := machine.savedStateRequirement()
	// Host user IDs change during migration; guest identity and generations do not.
	requirement.MigrationCompatibility = &compatibility
	snapshot, err := loadSavedState(directory, requirement)
	if err != nil {
		return fmt.Errorf("validate migrated snapshot: %w", err)
	}
	metadata := input.Specification.MetadataServiceData(input.ID, input.NetworkInterface.GuestIPAddress, input.NetworkInterface.MACAddress)
	if err := runtime.launchMemorySnapshot(ctx, input, "", snapshot.StatePath, snapshot.MemoryPath, metadata, false); err != nil {
		return err
	}
	machine.recordImageUse()
	return nil
}

func newMigrationSnapshotCompatibility(ctx context.Context, executable string, specification vm.Specification) (migrationSnapshotCompatibility, error) {
	host, err := migrationHostFingerprint(ctx, executable)
	if err != nil {
		return migrationSnapshotCompatibility{}, err
	}
	return migrationSnapshotCompatibility{
		Host: host,
		MemorySnapshotConfiguration: vm.MemorySnapshotConfiguration{
			VirtualCPUCount: specification.VirtualCPUCount(), MemoryMiB: specification.MemoryMiB, DiskMiB: specification.DiskMiB,
		},
	}, nil
}

func migrationHostFingerprint(ctx context.Context, executable string) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	binary, err := os.Open(executable)
	if err != nil {
		return "", fmt.Errorf("open Firecracker for snapshot compatibility: %w", err)
	}
	defer binary.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, binary); err != nil {
		return "", err
	}
	var kernel unix.Utsname
	if err := unix.Uname(&kernel); err != nil {
		return "", err
	}
	fmt.Fprintln(hash, runtime.GOARCH, unix.ByteSliceToString(kernel.Release[:]))
	cpu, err := os.ReadFile("/proc/cpuinfo")
	if err != nil {
		return "", err
	}
	// ponytail: require the same CPU features and kernel; use a compatibility matrix if needed.
	for line := range strings.SplitSeq(strings.SplitN(string(cpu), "\n\n", 2)[0], "\n") {
		key, value, _ := strings.Cut(line, ":")
		switch strings.TrimSpace(key) {
		case "vendor_id", "cpu family", "model", "model name", "stepping", "microcode", "flags", "Features", "CPU implementer", "CPU architecture", "CPU variant", "CPU part", "CPU revision":
			fmt.Fprintln(hash, strings.TrimSpace(key), strings.Join(strings.Fields(value), " "))
		}
	}
	return hex.EncodeToString(hash.Sum(nil)), ctx.Err()
}
