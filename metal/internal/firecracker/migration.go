package firecracker

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"runtime"
	"slices"
	"strings"

	"golang.org/x/sys/unix"

	"github.com/frappe/atlas/metal/internal/vm"
)

type migrationSnapshotCompatibility struct {
	Host string                    `json:"host"`
	CPU  migrationCPUCompatibility `json:"cpu"`
	vm.MemorySnapshotConfiguration
}

type migrationCPUCompatibility struct {
	Vendor   string `json:"vendor"`
	Model    string `json:"model"`
	Features string `json:"features"`
}

func (source migrationSnapshotCompatibility) validate(destination migrationSnapshotCompatibility) error {
	if source.MemorySnapshotConfiguration != destination.MemorySnapshotConfiguration {
		return errors.New("saved state VM shape does not match the destination")
	}
	return nil
}

// RestoreMigrationSnapshot validates transferred state and loads it paused into a new jail.
func (runtime *Runtime) RestoreMigrationSnapshot(ctx context.Context, input vm.RuntimeMachine, directory string) error {
	compatibility := migrationSnapshotCompatibility{
		MemorySnapshotConfiguration: vm.MemorySnapshotConfiguration{
			VirtualCPUCount: input.Specification.VirtualCPUCount(), MemoryMiB: input.Specification.MemoryMiB, DiskMiB: input.Specification.DiskMiB,
		},
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
	compatibility, err := migrationHostCompatibility(ctx, executable)
	if err != nil {
		return migrationSnapshotCompatibility{}, err
	}
	compatibility.MemorySnapshotConfiguration = vm.MemorySnapshotConfiguration{
		VirtualCPUCount: specification.VirtualCPUCount(), MemoryMiB: specification.MemoryMiB, DiskMiB: specification.DiskMiB,
	}
	return compatibility, nil
}

func migrationHostCompatibility(ctx context.Context, executable string) (migrationSnapshotCompatibility, error) {
	var compatibility migrationSnapshotCompatibility
	if err := ctx.Err(); err != nil {
		return compatibility, err
	}
	binary, err := os.Open(executable)
	if err != nil {
		return compatibility, fmt.Errorf("open Firecracker for snapshot compatibility: %w", err)
	}
	defer binary.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, binary); err != nil {
		return compatibility, err
	}
	var kernel unix.Utsname
	if err := unix.Uname(&kernel); err != nil {
		return compatibility, err
	}
	fmt.Fprintln(hash, runtime.GOARCH, unix.ByteSliceToString(kernel.Release[:]))
	compatibility.Host = hex.EncodeToString(hash.Sum(nil))
	cpu, err := os.ReadFile("/proc/cpuinfo")
	if err != nil {
		return compatibility, err
	}
	processor, err := migrationCPUFromInfo(string(cpu), runtime.GOARCH)
	if err != nil {
		return compatibility, err
	}
	compatibility.CPU = processor
	return compatibility, ctx.Err()
}

func migrationCPUFromInfo(info, architecture string) (migrationCPUCompatibility, error) {
	fields := make(map[string]string)
	for line := range strings.SplitSeq(strings.SplitN(info, "\n\n", 2)[0], "\n") {
		key, value, found := strings.Cut(line, ":")
		if found {
			fields[strings.TrimSpace(key)] = strings.TrimSpace(value)
		}
	}
	var cpu migrationCPUCompatibility
	var model []string
	switch architecture {
	case "amd64":
		cpu.Vendor, cpu.Features = fields["vendor_id"], fields["flags"]
		model = []string{fields["cpu family"], fields["model"]}
	case "arm64":
		cpu.Vendor, cpu.Features = fields["CPU implementer"], fields["Features"]
		// ARM CPU ID registers must remain compatible when KVM restores them.
		model = []string{fields["CPU architecture"], fields["CPU part"], fields["CPU variant"], fields["CPU revision"]}
	default:
		return cpu, fmt.Errorf("unsupported snapshot CPU architecture %s", architecture)
	}
	if cpu.Vendor == "" || cpu.Features == "" || slices.Contains(model, "") {
		return cpu, errors.New("CPU compatibility fields are missing from /proc/cpuinfo")
	}
	cpu.Model = strings.Join(model, "/")
	features := strings.Fields(cpu.Features)
	slices.Sort(features)
	cpu.Features = strings.Join(slices.Compact(features), " ")
	return cpu, nil
}
