package reconciler

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

type recordingDriver struct {
	ids     []string
	listErr error

	mu   sync.Mutex
	seen map[string]int
	done chan string
}

func newRecordingDriver(ids ...string) *recordingDriver {
	return &recordingDriver{ids: ids, seen: map[string]int{}, done: make(chan string, 16)}
}

func (d *recordingDriver) IDs(context.Context) ([]string, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.ids, d.listErr
}

func (d *recordingDriver) Reconcile(_ context.Context, id string) error {
	d.mu.Lock()
	d.seen[id]++
	d.mu.Unlock()
	d.done <- id
	return nil
}

func TestSweepReconcilesEveryVM(t *testing.T) {
	driver := newRecordingDriver("a", "b")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go New(driver, time.Hour, Config{}).Run(ctx)

	got := map[string]bool{}
	for range driver.ids {
		select {
		case id := <-driver.done:
			got[id] = true
		case <-time.After(2 * time.Second):
			t.Fatal("timed out waiting for the start pass")
		}
	}
	if !got["a"] || !got["b"] {
		t.Fatalf("reconciled = %v, want a and b", got)
	}
}

func TestWakeTriggersSweep(t *testing.T) {
	driver := newRecordingDriver("a")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	reconciler := New(driver, time.Hour, Config{})
	go reconciler.Run(ctx)

	<-driver.done
	reconciler.Wake()
	select {
	case <-driver.done:
	case <-time.After(2 * time.Second):
		t.Fatal("Wake did not trigger a pass")
	}
}

func TestSweepSurvivesListError(t *testing.T) {
	driver := newRecordingDriver("a")
	driver.listErr = errors.New("list failed")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	reconciler := New(driver, time.Hour, Config{})
	go reconciler.Run(ctx)

	driver.mu.Lock()
	driver.listErr = nil
	driver.mu.Unlock()
	reconciler.Wake()
	select {
	case <-driver.done:
	case <-time.After(2 * time.Second):
		t.Fatal("loop stopped after a list error")
	}
}

type concurrencyDriver struct {
	ids     []string
	started chan string
	release chan struct{}

	mutex           sync.Mutex
	active          int
	maximumObserved int
}

func (d *concurrencyDriver) IDs(context.Context) ([]string, error) { return d.ids, nil }

func (d *concurrencyDriver) Reconcile(ctx context.Context, id string) error {
	d.mutex.Lock()
	d.active++
	if d.active > d.maximumObserved {
		d.maximumObserved = d.active
	}
	d.mutex.Unlock()

	d.started <- id
	select {
	case <-d.release:
	case <-ctx.Done():
	}

	d.mutex.Lock()
	d.active--
	d.mutex.Unlock()
	return ctx.Err()
}

func TestSweepBoundsConcurrentOperations(t *testing.T) {
	driver := &concurrencyDriver{
		ids:     []string{"a", "b", "c", "d"},
		started: make(chan string, 4),
		release: make(chan struct{}),
	}
	reconciler := New(driver, time.Hour, Config{
		MaxConcurrentOperations: 2,
		OperationTimeout:        time.Second,
	})
	done := make(chan struct{})
	go func() {
		reconciler.reconcileAll(context.Background())
		close(done)
	}()

	<-driver.started
	<-driver.started
	select {
	case id := <-driver.started:
		t.Fatalf("operation %s started above the limit", id)
	case <-time.After(20 * time.Millisecond):
	}

	for range 2 {
		driver.release <- struct{}{}
	}
	<-driver.started
	<-driver.started
	for range 2 {
		driver.release <- struct{}{}
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("reconcile pass did not finish")
	}
	if driver.maximumObserved != 2 {
		t.Fatalf("maximum concurrent operations = %d, want 2", driver.maximumObserved)
	}
}

type timeoutDriver struct {
	timedOut chan struct{}
}

func (d *timeoutDriver) IDs(context.Context) ([]string, error) { return []string{"a"}, nil }

func (d *timeoutDriver) Reconcile(ctx context.Context, _ string) error {
	<-ctx.Done()
	close(d.timedOut)
	return ctx.Err()
}

func TestSweepAppliesOperationTimeout(t *testing.T) {
	driver := &timeoutDriver{timedOut: make(chan struct{})}
	reconciler := New(driver, time.Hour, Config{
		MaxConcurrentOperations: 1,
		OperationTimeout:        20 * time.Millisecond,
	})

	reconciler.reconcileAll(context.Background())
	select {
	case <-driver.timedOut:
	default:
		t.Fatal("reconcile operation did not time out")
	}
}
