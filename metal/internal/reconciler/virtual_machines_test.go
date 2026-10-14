package reconciler

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

type recordingManager struct {
	ids     []string
	listErr error

	mu   sync.Mutex
	seen map[string]int
	done chan string
}

func newRecordingManager(ids ...string) *recordingManager {
	return &recordingManager{ids: ids, seen: map[string]int{}, done: make(chan string, 16)}
}

func (manager *recordingManager) ListIDs(context.Context) ([]string, error) {
	manager.mu.Lock()
	defer manager.mu.Unlock()
	return manager.ids, manager.listErr
}

func (manager *recordingManager) Reconcile(_ context.Context, id string) error {
	manager.mu.Lock()
	manager.seen[id]++
	manager.mu.Unlock()
	manager.done <- id
	return nil
}

func TestRunReconcilesEveryVirtualMachine(t *testing.T) {
	manager := newRecordingManager("a", "b")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go NewVirtualMachineReconciler(manager, time.Hour, VirtualMachineConfig{Telemetry: telemetry.NewRegistry(nil)}).Run(ctx)

	got := map[string]bool{}
	for range manager.ids {
		select {
		case id := <-manager.done:
			got[id] = true
		case <-time.After(2 * time.Second):
			t.Fatal("timed out waiting for the start pass")
		}
	}
	if !got["a"] || !got["b"] {
		t.Fatalf("reconciled = %v, want a and b", got)
	}
}

func TestWakeRequestsReconciliation(t *testing.T) {
	manager := newRecordingManager("a")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	reconciler := NewVirtualMachineReconciler(manager, time.Hour, VirtualMachineConfig{Telemetry: telemetry.NewRegistry(nil)})
	go reconciler.Run(ctx)

	<-manager.done
	reconciler.Wake()
	select {
	case <-manager.done:
	case <-time.After(2 * time.Second):
		t.Fatal("Wake did not trigger a pass")
	}
}

func TestRunContinuesAfterListError(t *testing.T) {
	manager := newRecordingManager("a")
	manager.listErr = errors.New("list failed")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	reconciler := NewVirtualMachineReconciler(manager, time.Hour, VirtualMachineConfig{Telemetry: telemetry.NewRegistry(nil)})
	go reconciler.Run(ctx)

	manager.mu.Lock()
	manager.listErr = nil
	manager.mu.Unlock()
	reconciler.Wake()
	select {
	case <-manager.done:
	case <-time.After(2 * time.Second):
		t.Fatal("loop stopped after a list error")
	}
}

type concurrencyManager struct {
	ids     []string
	started chan string
	release chan struct{}

	mutex           sync.Mutex
	active          int
	maximumObserved int
}

func (manager *concurrencyManager) ListIDs(context.Context) ([]string, error) {
	return manager.ids, nil
}

func (manager *concurrencyManager) Reconcile(ctx context.Context, id string) error {
	manager.mutex.Lock()
	manager.active++
	if manager.active > manager.maximumObserved {
		manager.maximumObserved = manager.active
	}
	manager.mutex.Unlock()

	manager.started <- id
	select {
	case <-manager.release:
	case <-ctx.Done():
	}

	manager.mutex.Lock()
	manager.active--
	manager.mutex.Unlock()
	return ctx.Err()
}

func TestReconcileAllLimitsConcurrentOperations(t *testing.T) {
	manager := &concurrencyManager{
		ids:     []string{"a", "b", "c", "d"},
		started: make(chan string, 4),
		release: make(chan struct{}),
	}
	reconciler := NewVirtualMachineReconciler(manager, time.Hour, VirtualMachineConfig{
		Telemetry:               telemetry.NewRegistry(nil),
		MaxConcurrentOperations: 2,
		OperationTimeout:        time.Second,
	})
	done := make(chan struct{})
	go func() {
		reconciler.reconcileAll(context.Background())
		close(done)
	}()

	<-manager.started
	<-manager.started
	select {
	case id := <-manager.started:
		t.Fatalf("operation %s started above the limit", id)
	case <-time.After(20 * time.Millisecond):
	}

	for range 2 {
		manager.release <- struct{}{}
	}
	<-manager.started
	<-manager.started
	for range 2 {
		manager.release <- struct{}{}
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("reconcile pass did not finish")
	}
	if manager.maximumObserved != 2 {
		t.Fatalf("maximum concurrent operations = %d, want 2", manager.maximumObserved)
	}
}

type timeoutManager struct {
	timedOut chan struct{}
}

func (manager *timeoutManager) ListIDs(context.Context) ([]string, error) { return []string{"a"}, nil }

func (manager *timeoutManager) Reconcile(ctx context.Context, _ string) error {
	<-ctx.Done()
	close(manager.timedOut)
	return ctx.Err()
}

func TestReconcileAllAppliesOperationTimeout(t *testing.T) {
	manager := &timeoutManager{timedOut: make(chan struct{})}
	reconciler := NewVirtualMachineReconciler(manager, time.Hour, VirtualMachineConfig{
		Telemetry:               telemetry.NewRegistry(nil),
		MaxConcurrentOperations: 1,
		OperationTimeout:        20 * time.Millisecond,
	})

	reconciler.reconcileAll(context.Background())
	select {
	case <-manager.timedOut:
	default:
		t.Fatal("reconcile operation did not time out")
	}
}
