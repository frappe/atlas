// Package reconciler applies desired virtual machine and image state.
package reconciler

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

const (
	// defaultMaxConcurrentOperations limits active VM operations in one pass.
	defaultMaxConcurrentOperations = 4

	// defaultOperationTimeout bounds one list or reconcile operation. One
	// reconcile can download and import an image, so the limit is generous.
	defaultOperationTimeout = 35 * time.Minute
)

// VirtualMachineManager lists and reconciles virtual machine records.
type VirtualMachineManager interface {
	ListIDs(ctx context.Context) ([]string, error)
	Reconcile(ctx context.Context, id string) error
}

// VirtualMachineConfig controls reconciliation work within one pass.
type VirtualMachineConfig struct {
	// Telemetry counts passes and failures. It is required.
	Telemetry *telemetry.Registry
	// Logger receives reconciliation errors.
	Logger *slog.Logger
	// MaxConcurrentOperations limits active VM operations in one pass.
	MaxConcurrentOperations int
	// OperationTimeout limits one list or reconcile operation.
	OperationTimeout time.Duration
}

// VirtualMachineReconciler drives every VM towards its desired record.
type VirtualMachineReconciler struct {
	passScheduler

	manager                 VirtualMachineManager
	maxConcurrentOperations int
	operationTimeout        time.Duration
	logger                  *slog.Logger
}

// NewVirtualMachineReconciler returns a reconciler that runs at interval.
func NewVirtualMachineReconciler(
	manager VirtualMachineManager,
	interval time.Duration,
	configuration VirtualMachineConfig,
) *VirtualMachineReconciler {
	if configuration.MaxConcurrentOperations <= 0 {
		configuration.MaxConcurrentOperations = defaultMaxConcurrentOperations
	}
	if configuration.OperationTimeout <= 0 {
		configuration.OperationTimeout = defaultOperationTimeout
	}
	if configuration.Logger == nil {
		configuration.Logger = slog.Default()
	}

	return &VirtualMachineReconciler{
		passScheduler:           newPassScheduler("virtual_machine", interval, configuration.Telemetry),
		manager:                 manager,
		maxConcurrentOperations: configuration.MaxConcurrentOperations,
		operationTimeout:        configuration.OperationTimeout,
		logger:                  configuration.Logger,
	}
}

// Run reconciles every VM until ctx is canceled.
func (r *VirtualMachineReconciler) Run(ctx context.Context) {
	r.run(ctx, r.reconcileAll)
}

// reconcileAll reconciles every known VM with a bounded number of workers.
func (r *VirtualMachineReconciler) reconcileAll(ctx context.Context) {
	listContext, cancelList := context.WithTimeout(ctx, r.operationTimeout)
	ids, err := r.manager.ListIDs(listContext)
	cancelList()

	if err != nil {
		r.logFailure(ctx, "reconciler list failed", err)
		return
	}
	if len(ids) == 0 {
		return
	}

	jobs := make(chan string)
	workerCount := min(len(ids), r.maxConcurrentOperations)

	var workers sync.WaitGroup
	workers.Add(workerCount)
	for range workerCount {
		go func() {
			defer workers.Done()
			for id := range jobs {
				r.reconcile(ctx, id)
			}
		}()
	}

	defer func() {
		close(jobs)
		workers.Wait()
	}()

	for _, id := range ids {
		select {
		case jobs <- id:
		case <-ctx.Done():
			return
		}
	}
}

// reconcile applies one VM's desired record under the operation timeout.
func (r *VirtualMachineReconciler) reconcile(ctx context.Context, id string) {
	operationContext, cancel := context.WithTimeout(ctx, r.operationTimeout)
	defer cancel()

	if err := r.manager.Reconcile(operationContext, id); err != nil {
		r.logFailure(ctx, "virtual machine reconciliation failed", err, "virtual_machine_id", id)
	}
}

// logFailure reports an error unless ctx ended, because a canceled pass fails
// every operation still in flight and those errors say nothing about the host.
func (r *VirtualMachineReconciler) logFailure(ctx context.Context, message string, err error, fields ...any) {
	if ctx.Err() != nil {
		return
	}

	r.countFailure()
	r.logger.Error(message, append([]any{"error", err}, fields...)...)
}
