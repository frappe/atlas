package firecracker

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
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

// HostCompatibility probes the configured runtime for the controller sync response.
func (service *Runtime) HostCompatibility(ctx context.Context) (vm.HostCompatibility, error) {
	result, err := hostCompatibility(ctx, service.configuration.FirecrackerBin)
	if err != nil {
		return vm.HostCompatibility{}, err
	}
	result.CPUProfile, err = service.cpuProfile()
	if err != nil {
		return vm.HostCompatibility{}, err
	}
	if result.Architecture == "amd64" {
		result.TSCKHz, result.TSCScaling, err = migrationTSC()
	}
	return result, err
}

func (runtime *Runtime) cpuProfile() (string, error) {
	if len(runtime.configuration.CPUTemplate) == 0 {
		return "native", nil
	}
	var template map[string]any
	decoder := json.NewDecoder(bytes.NewReader(runtime.configuration.CPUTemplate))
	decoder.UseNumber()
	if err := decoder.Decode(&template); err != nil {
		return "", err
	}
	content, err := json.Marshal(template)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(content)
	return "custom:" + hex.EncodeToString(digest[:]), nil
}

func migrationTSC() (int, bool, error) {
	kvm, err := unix.Open("/dev/kvm", unix.O_RDWR|unix.O_CLOEXEC, 0)
	if err != nil {
		return 0, false, err
	}
	defer unix.Close(kvm)
	// KVM_CHECK_EXTENSION(KVM_CAP_TSC_CONTROL), KVM_CREATE_VM and KVM_CREATE_VCPU.
	scaling, _, errno := unix.Syscall(unix.SYS_IOCTL, uintptr(kvm), 0xae03, 60)
	if errno != 0 {
		return 0, false, errno
	}
	virtualMachine, err := unix.IoctlRetInt(kvm, 0xae01)
	if err != nil {
		return 0, false, err
	}
	defer unix.Close(virtualMachine)
	virtualCPU, err := unix.IoctlRetInt(virtualMachine, 0xae41)
	if err != nil {
		return 0, false, err
	}
	defer unix.Close(virtualCPU)
	frequency, err := unix.IoctlRetInt(virtualCPU, 0xaea3) // KVM_GET_TSC_KHZ.
	// Unstable or unavailable TSC blocks memory migration, not host sync.
	if errors.Is(err, unix.EIO) || errors.Is(err, unix.ENOTTY) {
		return 0, scaling > 0, nil
	}
	return frequency, scaling > 0, err
}

type migrationSnapshotCompatibility struct {
	// Host and CPU preserve the metadata schema of existing snapshots.
	Host string                     `json:"host,omitempty"`
	CPU  *migrationCPUCompatibility `json:"cpu,omitempty"`
	vm.MemorySnapshotConfiguration
}

type migrationCPUCompatibility struct {
	Vendor   string `json:"vendor"`
	Model    string `json:"model"`
	Features string `json:"features"`
}

// RestoreMigrationSnapshot validates transferred state and loads it paused into a new jail.
func (runtime *Runtime) RestoreMigrationSnapshot(ctx context.Context, input vm.RuntimeMachine, directory string) error {
	compatibility := migrationSnapshotCompatibility{
		MemorySnapshotConfiguration: vm.MemorySnapshotConfiguration{
			VirtualCPUCount: input.Specification.VirtualCPUCount(),
			MemoryMiB:       input.Specification.MemoryMiB,
			DiskMiB:         input.Specification.DiskMiB,
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
	if err := snapshot.restoreNetwork(ctx, input.NetworkInterface); err != nil {
		return err
	}
	metadata := input.Specification.MetadataServiceData(input.ID, input.NetworkInterface.GuestIPAddress, input.NetworkInterface.MACAddress)
	if err := runtime.launchMemorySnapshot(ctx, input, "", snapshot.StatePath, snapshot.MemoryPath, metadata, false); err != nil {
		return err
	}
	machine.recordImageUse()
	return nil
}

func hostCompatibility(ctx context.Context, executable string) (vm.HostCompatibility, error) {
	compatibility := vm.HostCompatibility{Architecture: runtime.GOARCH}
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
	compatibility.SnapshotHost = hex.EncodeToString(hash.Sum(nil))
	cpu, err := os.ReadFile("/proc/cpuinfo")
	if err != nil {
		return compatibility, err
	}
	processor, err := migrationCPUFromInfo(string(cpu), runtime.GOARCH)
	if err != nil {
		return compatibility, err
	}
	compatibility.CPUVendor = processor.Vendor
	compatibility.CPUModel = processor.Model
	compatibility.CPUFeatures = processor.Features
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
