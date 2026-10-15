package reconciler

import (
	"context"
	"log/slog"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// defaultMigrationOperationTimeout bounds one handshake and record write.
const defaultMigrationOperationTimeout = 5 * time.Minute

// MigrationDriver advances active destination migrations and releases source locks
// whose destination stopped answering.
type MigrationDriver interface {
	ActiveDestinationVirtualMachineIDs(ctx context.Context) ([]string, error)
	AdvanceDestination(ctx context.Context, virtualMachineID string) error
	ExpiredSourceVirtualMachineIDs(ctx context.Context) ([]string, error)
	ExpireSource(ctx context.Context, virtualMachineID string) error
}

// MigrationConfig controls one migration reconcile pass.
type MigrationConfig struct {
	// Telemetry counts passes and failures. It is required.
	Telemetry        *telemetry.Registry
	Logger           *slog.Logger
	OperationTimeout time.Duration
}

// MigrationReconciler advances destination migrations on an interval or on demand.
type MigrationReconciler struct {
	passScheduler

	driver           MigrationDriver
	operationTimeout time.Duration
	logger           *slog.Logger
}

// NewMigrationReconciler returns an interval-based migration reconciler.
func NewMigrationReconciler(driver MigrationDriver, interval time.Duration, configuration MigrationConfig) *MigrationReconciler {
	if configuration.OperationTimeout <= 0 {
		configuration.OperationTimeout = defaultMigrationOperationTimeout
	}
	if configuration.Logger == nil {
		configuration.Logger = slog.Default()
	}
	return &MigrationReconciler{
		passScheduler:    newPassScheduler("migration", interval, configuration.Telemetry),
		driver:           driver,
		operationTimeout: configuration.OperationTimeout,
		logger:           configuration.Logger,
	}
}

// Run advances migrations until ctx is canceled.
func (r *MigrationReconciler) Run(ctx context.Context) {
	r.run(ctx, r.pass)
}

// pass advances every destination migration, then releases every expired source lock.
func (r *MigrationReconciler) pass(ctx context.Context) {
	r.advanceAll(ctx)
	r.expireAll(ctx)
}

// advanceAll advances every active destination migration once.
func (r *MigrationReconciler) advanceAll(ctx context.Context) {
	virtualMachineIDs, err := r.list(ctx, r.driver.ActiveDestinationVirtualMachineIDs)
	if err != nil {
		r.logFailure(ctx, "migration reconciler list failed", err)
		return
	}

	for _, virtualMachineID := range virtualMachineIDs {
		if ctx.Err() != nil {
			return
		}
		r.advance(ctx, virtualMachineID)
	}
}

// expireAll releases the source locks whose destination stopped answering.
func (r *MigrationReconciler) expireAll(ctx context.Context) {
	virtualMachineIDs, err := r.list(ctx, r.driver.ExpiredSourceVirtualMachineIDs)
	if err != nil {
		r.logFailure(ctx, "migration reconciler source list failed", err)
		return
	}

	for _, virtualMachineID := range virtualMachineIDs {
		if ctx.Err() != nil {
			return
		}
		operationContext, cancel := context.WithTimeout(ctx, r.operationTimeout)
		err := r.driver.ExpireSource(operationContext, virtualMachineID)
		cancel()
		if err != nil {
			r.logFailure(ctx, "migration source expiry failed", err, "virtual_machine_id", virtualMachineID)
		}
	}
}

// list runs one bounded listing.
func (r *MigrationReconciler) list(ctx context.Context, read func(context.Context) ([]string, error)) ([]string, error) {
	listContext, cancel := context.WithTimeout(ctx, r.operationTimeout)
	defer cancel()
	return read(listContext)
}

// advance runs one migration handshake step under the operation timeout.
func (r *MigrationReconciler) advance(ctx context.Context, virtualMachineID string) {
	operationContext, cancel := context.WithTimeout(ctx, r.operationTimeout)
	defer cancel()

	if err := r.driver.AdvanceDestination(operationContext, virtualMachineID); err != nil {
		r.logFailure(ctx, "migration handshake failed", err, "virtual_machine_id", virtualMachineID)
	}
}

// logFailure reports errors unless cancellation caused them.
func (r *MigrationReconciler) logFailure(ctx context.Context, message string, err error, fields ...any) {
	if ctx.Err() != nil {
		return
	}
	r.countFailure()
	r.logger.Error(message, append([]any{"error", err}, fields...)...)
}
