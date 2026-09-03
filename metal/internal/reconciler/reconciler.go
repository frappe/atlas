// Package reconciler applies desired virtual machine and image state.
package reconciler

import (
	"context"
	"log"
	"sync"
	"time"
)

// Driver reconciles virtual machine reservations.
type Driver interface {
	IDs(ctx context.Context) ([]string, error)
	Reconcile(ctx context.Context, id string) error
}

const (
	defaultMaxConcurrentOperations = 4
	defaultOperationTimeout        = 35 * time.Minute
)

// Config controls reconciliation work within one pass.
type Config struct {
	// MaxConcurrentOperations limits active VM operations in one pass.
	MaxConcurrentOperations int
	// OperationTimeout limits one list or reconcile operation.
	OperationTimeout time.Duration
}

// Reconciler runs reconciliation at an interval and on demand.
type Reconciler struct {
	driver                  Driver
	interval                time.Duration
	wake                    chan struct{}
	maxConcurrentOperations int
	operationTimeout        time.Duration
}

// New returns a Reconciler with the specified interval.
func New(driver Driver, interval time.Duration, configuration Config) *Reconciler {
	if configuration.MaxConcurrentOperations <= 0 {
		configuration.MaxConcurrentOperations = defaultMaxConcurrentOperations
	}
	if configuration.OperationTimeout <= 0 {
		configuration.OperationTimeout = defaultOperationTimeout
	}
	return &Reconciler{
		driver:                  driver,
		interval:                interval,
		wake:                    make(chan struct{}, 1),
		maxConcurrentOperations: configuration.MaxConcurrentOperations,
		operationTimeout:        configuration.OperationTimeout,
	}
}

// Wake requests a reconcile pass without blocking the caller.
func (r *Reconciler) Wake() {
	select {
	case r.wake <- struct{}{}:
	default:
	}
}

// Run reconciles until the context is canceled.
func (r *Reconciler) Run(ctx context.Context) {
	ticker := time.NewTicker(r.interval)
	defer ticker.Stop()
	for {
		r.reconcileAll(ctx)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		case <-r.wake:
		}
	}
}

func (r *Reconciler) reconcileAll(ctx context.Context) {
	listContext, cancelList := context.WithTimeout(ctx, r.operationTimeout)
	ids, err := r.driver.IDs(listContext)
	cancelList()
	if err != nil {
		if ctx.Err() == nil {
			log.Printf("reconciler: list: %v", err)
		}
		return
	}
	if len(ids) == 0 {
		return
	}

	workerCount := min(len(ids), r.maxConcurrentOperations)
	jobs := make(chan string)
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

	for _, id := range ids {
		select {
		case jobs <- id:
		case <-ctx.Done():
			close(jobs)
			workers.Wait()
			return
		}
	}
	close(jobs)
	workers.Wait()
}

func (r *Reconciler) reconcile(ctx context.Context, id string) {
	operationContext, cancel := context.WithTimeout(ctx, r.operationTimeout)
	defer cancel()
	if err := r.driver.Reconcile(operationContext, id); err != nil && ctx.Err() == nil {
		log.Printf("reconciler: vm %s: %v", id, err)
	}
}
