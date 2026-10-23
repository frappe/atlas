package reconciler

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

type recordingDriver struct {
	ids        []string
	listErr    error
	expiredIDs []string

	mu      sync.Mutex
	seen    map[string]int
	expired map[string]int
	done    chan string
}

func newRecordingDriver(ids ...string) *recordingDriver {
	return &recordingDriver{ids: ids, seen: map[string]int{}, expired: map[string]int{}, done: make(chan string, 16)}
}

func (driver *recordingDriver) ActiveDestinationVirtualMachineIDs(context.Context) ([]string, error) {
	driver.mu.Lock()
	defer driver.mu.Unlock()
	return driver.ids, driver.listErr
}

func (driver *recordingDriver) AdvanceDestination(_ context.Context, virtualMachineID string) error {
	driver.mu.Lock()
	driver.seen[virtualMachineID]++
	driver.mu.Unlock()
	driver.done <- virtualMachineID
	return nil
}

func (driver *recordingDriver) ExpiredSourceVirtualMachineIDs(context.Context) ([]string, error) {
	driver.mu.Lock()
	defer driver.mu.Unlock()
	return driver.expiredIDs, nil
}

func (driver *recordingDriver) ExpireSource(_ context.Context, virtualMachineID string) error {
	driver.mu.Lock()
	driver.expired[virtualMachineID]++
	driver.mu.Unlock()
	driver.done <- virtualMachineID
	return nil
}

func (*recordingDriver) RemoveAbortedDestinations(context.Context) error { return nil }

func TestRunReleasesEveryExpiredSourceLock(t *testing.T) {
	driver := newRecordingDriver()
	driver.expiredIDs = []string{"vm-9"}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go NewMigrationReconciler(driver, time.Hour, MigrationConfig{Telemetry: telemetry.NewRegistry(nil)}).Run(ctx)

	select {
	case <-driver.done:
	case <-time.After(5 * time.Second):
		t.Fatal("the reconciler did not release the expired source lock")
	}
	driver.mu.Lock()
	defer driver.mu.Unlock()
	if driver.expired["vm-9"] == 0 {
		t.Fatalf("expired = %v", driver.expired)
	}
}

func TestRunAdvancesEveryActiveMigration(t *testing.T) {
	driver := newRecordingDriver("vm-1", "vm-2")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go NewMigrationReconciler(driver, time.Hour, MigrationConfig{Telemetry: telemetry.NewRegistry(nil)}).Run(ctx)

	got := map[string]bool{}
	for range driver.ids {
		select {
		case id := <-driver.done:
			got[id] = true
		case <-time.After(2 * time.Second):
			t.Fatal("timed out waiting for the start pass")
		}
	}
	if !got["vm-1"] || !got["vm-2"] {
		t.Fatalf("advanced = %v, want vm-1 and vm-2", got)
	}
}

func TestWakeRequestsAMigrationPass(t *testing.T) {
	driver := newRecordingDriver("vm-1")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	reconciler := NewMigrationReconciler(driver, time.Hour, MigrationConfig{Telemetry: telemetry.NewRegistry(nil)})
	go reconciler.Run(ctx)

	<-driver.done
	reconciler.Wake()
	select {
	case <-driver.done:
	case <-time.After(2 * time.Second):
		t.Fatal("Wake did not trigger a pass")
	}
}

func TestMigrationRunContinuesAfterListError(t *testing.T) {
	driver := newRecordingDriver("vm-1")
	driver.listErr = errors.New("list failed")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	reconciler := NewMigrationReconciler(driver, time.Hour, MigrationConfig{Telemetry: telemetry.NewRegistry(nil)})
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
