package migration

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"

	"github.com/klauspost/compress/zstd"

	"github.com/frappe/atlas/metal/internal/vm"
)

// maximumKernelBytes bounds a received kernel. An Ubuntu vmlinux is about 70 MiB.
const maximumKernelBytes = 1 << 30

// kernelStore keeps kernels by SHA-256 value.
type kernelStore interface {
	HasKernel(kernelSHA256 string) (bool, error)
	OpenKernel(kernelSHA256 string) (*os.File, error)
	StoreKernel(kernelSHA256 string, source io.Reader) error
}

// OpenSourceKernel opens the kernel of the VM that this migration locked.
func (m *Manager) OpenSourceKernel(ctx context.Context, migrationID, virtualMachineID string) (*os.File, error) {
	if !vm.ValidIdentifier(virtualMachineID) {
		return nil, vm.ErrConflict
	}
	unlock, err := m.host.LockOperation(ctx, virtualMachineID)
	if err != nil {
		return nil, err
	}
	defer unlock()

	if _, err := m.boundSourceRecord(virtualMachineID, migrationID); err != nil {
		return nil, err
	}
	desired, err := m.host.ReadDesired(virtualMachineID)
	if err != nil {
		return nil, err
	}
	return m.kernels.OpenKernel(desired.Specification.Image.KernelSHA256)
}

// ReceiveKernel downloads the source kernel and passes it to store.
func (c *SourceClient) ReceiveKernel(ctx context.Context, address, migrationID, virtualMachineID string, store func(io.Reader) error) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, address+sourcePath(migrationID, virtualMachineID, "/kernel"), nil)
	if err != nil {
		return err
	}
	response, err := c.transferClient.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusNotFound {
		return vm.ErrNotFound
	}
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("source returned HTTP %d", response.StatusCode)
	}
	decoder, err := zstd.NewReader(response.Body)
	if err != nil {
		return err
	}
	defer decoder.Close()
	return store(io.LimitReader(decoder, maximumKernelBytes))
}

// receiveKernel copies the kernel from the source when this host does not have
// it. When the source does not send it, a cold boot downloads it from the kernel URL.
func (m *Manager) receiveKernel(ctx context.Context, record destinationRecord) error {
	desired, err := m.host.ReadDesired(record.VirtualMachineID)
	if err != nil {
		return err
	}
	kernelSHA256 := desired.Specification.Image.KernelSHA256
	hasKernel, err := m.kernels.HasKernel(kernelSHA256)
	if err != nil || hasKernel {
		return err
	}

	err = m.source.ReceiveKernel(ctx, record.Source, record.ID, record.VirtualMachineID, func(kernel io.Reader) error {
		return m.kernels.StoreKernel(kernelSHA256, kernel)
	})
	if errors.Is(err, vm.ErrNotFound) {
		m.logger.Warn("migration source did not send the kernel",
			"migration_id", record.ID, "virtual_machine_id", record.VirtualMachineID, "kernel_sha256", kernelSHA256)
		return nil
	}
	if err != nil {
		return fmt.Errorf("receive kernel: %w", err)
	}
	return nil
}
