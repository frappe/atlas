package storage

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/vm"
)

const rescueCheckpointProperty = "atlas:rescue-generation"

// CheckRescueCheckpointSpace checks quota-aware space outside the volume reservation.
// It does not reserve capacity. Call it again after stopping the guest, before creation.
func (store *VirtualMachineStore) CheckRescueCheckpointSpace(ctx context.Context, virtualMachineID string) (vm.RescueCheckpointSpace, error) {
	output, err := platform.Output(ctx, "zfs", "get", "-Hp", "-o", "value",
		"available,referenced,refreservation,usedbyrefreservation", store.pool.virtualMachineDataset(virtualMachineID))
	if err != nil {
		return vm.RescueCheckpointSpace{}, fmt.Errorf("read checkpoint capacity: %w", err)
	}
	values, err := parseCheckpointNumbers(output, 4)
	if err != nil {
		return vm.RescueCheckpointSpace{}, err
	}
	output, err = platform.Output(ctx, "zpool", "list", "-Hp", "-o", "size", store.pool.name)
	if err != nil {
		return vm.RescueCheckpointSpace{}, fmt.Errorf("read checkpoint pool size: %w", err)
	}
	poolSize, err := parseCheckpointNumbers(output, 1)
	if err != nil {
		return vm.RescueCheckpointSpace{}, err
	}
	space := checkpointSpace(values[0], values[1], values[2], values[3], poolSize[0])
	if space.AvailableBytes < space.RequiredBytes {
		return space, &vm.RescueCheckpointSpaceError{Space: space}
	}
	return space, nil
}

func checkpointSpace(available, referenced, reservation, unusedReservation, poolSize uint64) vm.RescueCheckpointSpace {
	// ZFS includes unused refreservation in the volume's available bytes. A
	// snapshot needs space outside that reservation, including ancestor quotas.
	outsideReservation := uint64(0)
	if available > unusedReservation {
		outsideReservation = available - unusedReservation
	}
	headroom := max(uint64(1<<30), poolSize/20)
	// Shared blocks can make the actual cost lower. This upper bound avoids
	// rejecting sparse or lightly written volumes based on their full size.
	cost := min(referenced, reservation)
	if cost > ^uint64(0)-headroom {
		return vm.RescueCheckpointSpace{AvailableBytes: outsideReservation, RequiredBytes: ^uint64(0)}
	}
	return vm.RescueCheckpointSpace{AvailableBytes: outsideReservation, RequiredBytes: cost + headroom}
}

func parseCheckpointNumbers(output string, count int) ([]uint64, error) {
	fields := strings.Fields(output)
	if len(fields) != count {
		return nil, fmt.Errorf("invalid rescue checkpoint capacity output")
	}
	values := make([]uint64, count)
	for index, field := range fields {
		value, err := strconv.ParseUint(field, 10, 64)
		if err != nil {
			return nil, fmt.Errorf("invalid rescue checkpoint capacity value: %w", err)
		}
		values[index] = value
	}
	return values, nil
}

// CreateRescueCheckpoint creates or adopts the snapshot for one accepted session.
// The caller must stop the guest and serialize VM storage operations. Once the
// caller has persisted the returned GUID, a missing snapshot must not be recreated.
func (store *VirtualMachineStore) CreateRescueCheckpoint(ctx context.Context, virtualMachineID string, generation uint64) (string, error) {
	if generation == 0 {
		return "", fmt.Errorf("rescue checkpoint requires a session generation")
	}
	snapshot := store.pool.rescueCheckpoint(virtualMachineID, generation)
	exists, err := datasetExists(ctx, snapshot)
	if err != nil {
		return "", err
	}
	if !exists {
		if _, err := store.CheckRescueCheckpointSpace(ctx, virtualMachineID); err != nil {
			return "", err
		}
		if err := platform.Run(ctx, "zfs", "snapshot", "-o",
			rescueCheckpointProperty+"="+strconv.FormatUint(generation, 10), snapshot); err != nil {
			return "", fmt.Errorf("create rescue checkpoint: %w", err)
		}
	}
	return store.RescueCheckpointGUID(ctx, virtualMachineID, generation)
}

// RescueCheckpointGUID verifies snapshot ownership and returns its immutable GUID.
// Absence is an error, including after an interrupted rollback.
func (store *VirtualMachineStore) RescueCheckpointGUID(ctx context.Context, virtualMachineID string, generation uint64) (string, error) {
	if generation == 0 {
		return "", fmt.Errorf("rescue checkpoint requires a session generation")
	}
	output, err := platform.Output(ctx, "zfs", "get", "-Hp", "-o", "property,value,source",
		"guid,"+rescueCheckpointProperty, store.pool.rescueCheckpoint(virtualMachineID, generation))
	if err != nil {
		if errors.Is(notFoundAware(err), ErrNotFound) {
			return "", errors.Join(ErrNotFound, vm.ErrNotFound)
		}
		return "", fmt.Errorf("inspect rescue checkpoint: %w", err)
	}
	guid, err := parseRescueCheckpointIdentity(output, generation)
	if err != nil {
		return "", err
	}
	value, err := strconv.ParseUint(guid, 10, 64)
	if err != nil || value == 0 {
		return "", fmt.Errorf("invalid rescue checkpoint GUID")
	}
	return guid, nil
}

func parseRescueCheckpointIdentity(output string, generation uint64) (string, error) {
	var guid string
	owned := false
	for _, line := range strings.Split(strings.TrimSpace(output), "\n") {
		fields := strings.Split(line, "\t")
		if len(fields) != 3 {
			return "", fmt.Errorf("invalid rescue checkpoint identity output")
		}
		switch fields[0] {
		case "guid":
			guid = fields[1]
		case rescueCheckpointProperty:
			owned = fields[1] == strconv.FormatUint(generation, 10) && fields[2] == "local"
		}
	}
	if guid == "" || !owned {
		return "", fmt.Errorf("rescue checkpoint identity does not match session %d", generation)
	}
	return guid, nil
}

// RollbackRescueCheckpoint restores the verified baseline without removing newer snapshots.
// The caller must keep the guest stopped until rollback completion is persisted.
func (store *VirtualMachineStore) RollbackRescueCheckpoint(ctx context.Context, virtualMachineID string, generation uint64, guid string) error {
	actual, err := store.RescueCheckpointGUID(ctx, virtualMachineID, generation)
	if err != nil {
		return err
	}
	if actual != guid {
		return fmt.Errorf("rescue checkpoint GUID changed")
	}
	if err := platform.Run(ctx, "zfs", "rollback", store.pool.rescueCheckpoint(virtualMachineID, generation)); err != nil {
		return fmt.Errorf("restore rescue checkpoint: %w", err)
	}
	return nil
}

// DiscardRescueCheckpoint deletes only the verified baseline. Absence is safe for
// cleanup, but the caller must persist rollback completion before requesting it.
func (store *VirtualMachineStore) DiscardRescueCheckpoint(ctx context.Context, virtualMachineID string, generation uint64, guid string) error {
	if generation == 0 || guid == "" {
		return fmt.Errorf("rescue checkpoint identity is required")
	}
	snapshot := store.pool.rescueCheckpoint(virtualMachineID, generation)
	exists, err := datasetExists(ctx, snapshot)
	if err != nil || !exists {
		return err
	}
	actual, err := store.RescueCheckpointGUID(ctx, virtualMachineID, generation)
	if err != nil {
		return err
	}
	if actual != guid {
		return fmt.Errorf("rescue checkpoint GUID changed")
	}
	if err := platform.Run(ctx, "zfs", "destroy", snapshot); err != nil {
		return fmt.Errorf("discard rescue checkpoint: %w", err)
	}
	return nil
}
