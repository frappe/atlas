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

// SleepTransferManager offloads snapshots independently of VM reconciliation.
type SleepTransferManager interface {
	VirtualMachineManager
	OffloadSleepArtifacts(ctx context.Context, id string) error
}

// SleepRestoreManager reconciles sleeping VMs independently of ordinary VM work.
type SleepRestoreManager interface {
	VirtualMachineManager
	RestoreSleepState(ctx context.Context, id string) error
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

	manager          VirtualMachineManager
	operation        func(context.Context, string) error
	operationTimeout time.Duration
	logger           *slog.Logger
	backoff          *backoff

	// slots bounds active operations across passes. A pass does not wait for
	// its operations, so one slow VM does not delay the next pass.
	slots         chan struct{}
	operations    sync.WaitGroup
	inFlightMutex sync.Mutex
	inFlight      map[string]bool
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
		passScheduler:    newPassScheduler("virtual_machine", interval, configuration.Telemetry),
		manager:          manager,
		operation:        manager.Reconcile,
		operationTimeout: configuration.OperationTimeout,
		logger:           configuration.Logger,
		backoff:          newBackoff(interval),
		slots:            make(chan struct{}, configuration.MaxConcurrentOperations),
		inFlight:         make(map[string]bool),
	}
}

// NewSleepTransferReconciler runs sleep transfers in their own bounded worker pool.
func NewSleepTransferReconciler(manager SleepTransferManager, interval time.Duration, configuration VirtualMachineConfig) *VirtualMachineReconciler {
	if configuration.MaxConcurrentOperations <= 0 {
		configuration.MaxConcurrentOperations = 2
	}
	reconciler := NewVirtualMachineReconciler(manager, interval, configuration)
	reconciler.loop = "sleep_transfer"
	reconciler.operation = manager.OffloadSleepArtifacts
	return reconciler
}

// NewSleepRestoreReconciler gives wake downloads their own bounded worker pool.
func NewSleepRestoreReconciler(manager SleepRestoreManager, interval time.Duration, configuration VirtualMachineConfig) *VirtualMachineReconciler {
	if configuration.MaxConcurrentOperations <= 0 {
		configuration.MaxConcurrentOperations = 2
	}

	reconciler := NewVirtualMachineReconciler(manager, interval, configuration)
	reconciler.loop = "sleep_restore"
	reconciler.operation = manager.RestoreSleepState
	return reconciler
}

// Wake requests a pass and clears every backoff, so a request does not wait for a retry time.
func (r *VirtualMachineReconciler) Wake() {
	r.backoff.reset()
	r.passScheduler.Wake()
}

// Run reconciles every VM until ctx is canceled, then waits for active operations.
func (r *VirtualMachineReconciler) Run(ctx context.Context) {
	r.run(ctx, r.reconcileAll)
	r.operations.Wait()
}

// reconcileAll starts an operation for every known VM that has none active. It
// waits for a free slot, not for the operations it starts.
func (r *VirtualMachineReconciler) reconcileAll(ctx context.Context) {
	listContext, cancelList := context.WithTimeout(ctx, r.operationTimeout)
	ids, err := r.manager.ListIDs(listContext)
	cancelList()

	if err != nil {
		r.logFailure(ctx, "reconciler list failed", err)
		return
	}

	for _, id := range ids {
		if !r.startOperation(id) {
			continue
		}
		select {
		case r.slots <- struct{}{}:
		case <-ctx.Done():
			r.finishOperation(id)
			return
		}
		r.operations.Go(func() {
			defer func() {
				<-r.slots
				r.finishOperation(id)
			}()
			r.reconcile(ctx, id)
		})
	}
}

// startOperation marks id active, or reports false when it already is.
func (r *VirtualMachineReconciler) startOperation(id string) bool {
	r.inFlightMutex.Lock()
	defer r.inFlightMutex.Unlock()
	if r.inFlight[id] {
		return false
	}
	r.inFlight[id] = true
	return true
}

func (r *VirtualMachineReconciler) finishOperation(id string) {
	r.inFlightMutex.Lock()
	delete(r.inFlight, id)
	r.inFlightMutex.Unlock()
}

// reconcile applies one VM's desired record under the operation timeout.
// A VM that failed waits for its backoff before the next attempt.
func (r *VirtualMachineReconciler) reconcile(ctx context.Context, id string) {
	if r.backoff.isWaiting(id) {
		return
	}

	operationContext, cancel := context.WithTimeout(ctx, r.operationTimeout)
	defer cancel()

	startedAt := r.backoff.now()
	if err := r.operation(operationContext, id); err != nil {
		if ctx.Err() == nil {
			r.backoff.fail(id, startedAt)
		}
		r.logFailure(ctx, "virtual machine reconciliation failed", err, "virtual_machine_id", id)
		return
	}
	r.backoff.succeed(id)
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
