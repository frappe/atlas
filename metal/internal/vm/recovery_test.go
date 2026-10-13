package vm

import (
	"context"
	"errors"
	"testing"
	"time"
)

func enterProtected(t *testing.T) (*Manager, *fakeRuntime, *fakeStorage) {
	t.Helper()
	manager, runtime, storage := rescueMachine(t)
	if err := manager.RequestRescue(context.Background(), "machine-1", testRescueImage(), RescueProtected); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(context.Background(), "machine-1"); err != nil {
		t.Fatal(err)
	}
	return manager, runtime, storage
}

func TestRecoveryKeepRetainsBaselineAcrossNormalBootAndReentry(t *testing.T) {
	m, _, s := enterProtected(t)
	ctx := context.Background()
	if err := m.RequestRescueExit(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := m.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	d, _ := m.store.readDesired("machine-1")
	baseline := d.Recovery
	if !baseline.HasCheckpoint() || baseline.Active || time.Until(baseline.ExpiresAt) < 11*time.Hour {
		t.Fatalf("recovery = %+v", baseline)
	}
	if err := m.RequestRescue(ctx, "machine-1", testRescueImage(), RescueProtected); err != nil {
		t.Fatal(err)
	}
	if err := m.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	d, _ = m.store.readDesired("machine-1")
	if d.Recovery.Generation != baseline.Generation || !d.Recovery.ExpiresAt.Equal(baseline.ExpiresAt) || s.checkpointCreates != 1 {
		t.Fatalf("replaced baseline: %+v, creates %d", d.Recovery, s.checkpointCreates)
	}
}

func TestRecoveryExpiryWaitsForActiveRescueExit(t *testing.T) {
	m, _, s := enterProtected(t)
	ctx := context.Background()
	d, _ := m.store.readDesired("machine-1")
	d.Recovery.ExpiresAt = time.Now().Add(-time.Hour)
	if err := m.store.writeDesired(d); err != nil {
		t.Fatal(err)
	}
	if err := m.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if s.checkpointDiscards != 0 {
		t.Fatal("expired active protection")
	}
	if err := m.RequestRescueExit(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := m.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	d, _ = m.store.readDesired("machine-1")
	if !d.Recovery.Complete || s.checkpointDiscards != 1 || s.checkpointRollbacks != 0 {
		t.Fatalf("expiry: %+v", d.Recovery)
	}
}

func TestRetainedRecoveryRollbackFailureKeepsGuestStoppedAndRetries(t *testing.T) {
	m, r, s := enterProtected(t)
	ctx := context.Background()
	if err := m.RequestRescueExit(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := m.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := m.RequestRecoveryAction(ctx, "machine-1", "rollback", 1); err != nil {
		t.Fatal(err)
	}
	if err := m.RequestRecoveryAction(ctx, "machine-1", "finish", 1); !errors.Is(err, ErrConflict) {
		t.Fatalf("conflicting finish: %v", err)
	}
	s.checkpointError = errors.New("restore failed")
	if err := m.Reconcile(ctx, "machine-1"); err == nil {
		t.Fatal("ignored restore failure")
	}
	if r.state != StateStopped || s.checkpointDiscards != 0 {
		t.Fatal("failed restore booted or lost checkpoint")
	}
	s.checkpointError = nil
	if err := m.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	d, _ := m.store.readDesired("machine-1")
	if !d.Recovery.RollbackApplied || !d.Recovery.Complete || r.state != StateRunning {
		t.Fatalf("rollback: %+v, %s", d.Recovery, r.state)
	}
	if err := m.RequestRecoveryAction(ctx, "machine-1", "rollback", 1); err != nil {
		t.Fatal(err)
	}
	if err := m.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if s.checkpointRollbacks != 1 {
		t.Fatal("repeated completed restore")
	}
}

func TestRecoverySpaceFailureDoesNotStopOriginalGuest(t *testing.T) {
	m, r, s := rescueMachine(t)
	ctx := context.Background()
	if err := m.RequestRescue(ctx, "machine-1", testRescueImage(), RescueProtected); err != nil {
		t.Fatal(err)
	}
	s.preflightError = errors.New("pool filled after request")
	if err := m.Reconcile(ctx, "machine-1"); err == nil {
		t.Fatal("ignored capacity failure")
	}
	if r.state != StateRunning || s.checkpointCreates != 0 {
		t.Fatal("failed preflight changed original guest")
	}
}

func TestRecoveryReadOnlyHasNoCheckpointAndCannotRollback(t *testing.T) {
	m, _, s := rescueMachine(t)
	ctx := context.Background()
	s.preflightError = errors.New("no space")
	if err := m.RequestRescue(ctx, "machine-1", testRescueImage(), RescueReadOnly); err != nil {
		t.Fatal(err)
	}
	if err := m.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if s.checkpointCreates != 0 {
		t.Fatal("read-only created checkpoint")
	}
	if err := m.RequestRecoveryAction(ctx, "machine-1", "rollback", 1); !errors.Is(err, ErrConflict) {
		t.Fatalf("unavailable rollback: %v", err)
	}
}

func TestRecoveryRollbackCompletionSurvivesCleanupFailure(t *testing.T) {
	m, _, s := enterProtected(t)
	ctx := context.Background()
	if err := m.RequestRecoveryAction(ctx, "machine-1", "rollback", 1); err != nil {
		t.Fatal(err)
	}
	// Simulate the durable point after successful restore but before cleanup.
	d, _ := m.store.readDesired("machine-1")
	d.Recovery.RollbackApplied = true
	if err := m.store.writeDesired(d); err != nil {
		t.Fatal(err)
	}
	if err := m.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if s.checkpointRollbacks != 0 || s.checkpointDiscards != 1 {
		t.Fatal("replayed completed restore")
	}
}

func TestRecoveryKeepAndRollbackCannotCompete(t *testing.T) {
	m, _, _ := enterProtected(t)
	ctx := context.Background()
	if err := m.RequestRescueExit(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := m.RequestRecoveryAction(ctx, "machine-1", "rollback", 1); !errors.Is(err, ErrConflict) {
		t.Fatalf("competing rollback: %v", err)
	}
	if err := m.RequestRescueExit(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := m.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := m.RequestRecoveryAction(ctx, "machine-1", "rollback", 1); err != nil {
		t.Fatal(err)
	}
}

func TestRecoveryExitAdoptsCheckpointCreatedBeforePersistence(t *testing.T) {
	m, _, s := enterProtected(t)
	ctx := context.Background()
	d, _ := m.store.readDesired("machine-1")
	d.Recovery.GUID = ""
	if err := m.store.writeDesired(d); err != nil {
		t.Fatal(err)
	}
	if err := m.RequestRescueExit(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := m.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	d, _ = m.store.readDesired("machine-1")
	if !d.Recovery.HasCheckpoint() || d.Recovery.GUID != s.checkpointGUID {
		t.Fatalf("lost checkpoint: %+v", d.Recovery)
	}
}

func TestRecoveryMissingCheckpointCannotRestoreButCanFinish(t *testing.T) {
	m, _, s := enterProtected(t)
	ctx := context.Background()
	if err := m.RequestRescueExit(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := m.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	s.checkpointGUID = ""
	if err := m.RequestRecoveryAction(ctx, "machine-1", "rollback", 1); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing checkpoint restore: %v", err)
	}
	if err := m.RequestRecoveryAction(ctx, "machine-1", "finish", 1); err != nil {
		t.Fatal(err)
	}
	if err := m.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	d, _ := m.store.readDesired("machine-1")
	if !d.Recovery.Complete || d.Recovery.RollbackApplied {
		t.Fatalf("invalid completion: %+v", d.Recovery)
	}
}

func TestRecoveryRejectsCorruptDurableState(t *testing.T) {
	for _, recovery := range []Recovery{
		{GUID: "42"}, {Generation: 1, GUID: "bad"}, {Generation: 1, Action: "unknown"},
		{Generation: 1, RollbackApplied: true}, {Generation: 1, Complete: true, Active: true},
	} {
		if recovery.validate() == nil {
			t.Fatalf("accepted %+v", recovery)
		}
	}
}

func TestRecoveryAdoptsCreatedCheckpointAfterCapacityFalls(t *testing.T) {
	m, _, s := enterProtected(t)
	ctx := context.Background()
	d, _ := m.store.readDesired("machine-1")
	d.Recovery.GUID = ""
	if err := m.store.writeDesired(d); err != nil {
		t.Fatal(err)
	}
	s.preflightError = errors.New("pool filled after snapshot creation")
	if err := m.preflightRecovery(ctx, d); err != nil {
		t.Fatal(err)
	}
}

func TestRecoveryRejectsConfirmationForAnotherSession(t *testing.T) {
	m, _, _ := enterProtected(t)
	ctx := context.Background()
	for _, generation := range []uint64{0, 2} {
		if err := m.RequestRecoveryAction(ctx, "machine-1", "rollback", generation); !errors.Is(err, ErrConflict) {
			t.Fatalf("accepted stale confirmation: %v", err)
		}
	}
	d, _ := m.store.readDesired("machine-1")
	if d.Recovery.Action != "" || !d.Specification.Rescue.Enabled {
		t.Fatal("stale request changed recovery")
	}
}

func TestRecoveryConcurrentDispositionsAcceptOnlyOne(t *testing.T) {
	manager, _, _ := enterProtected(t)
	ctx := context.Background()
	if err := manager.RequestRescueExit(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	type result struct {
		action string
		err    error
	}
	start := make(chan struct{})
	results := make(chan result, 2)
	for _, action := range []string{"rollback", "finish"} {
		go func() {
			<-start
			results <- result{action, manager.RequestRecoveryAction(ctx, "machine-1", action, 1)}
		}()
	}
	close(start)
	accepted := ""
	for range 2 {
		outcome := <-results
		if outcome.err == nil {
			if accepted != "" {
				t.Fatal("accepted competing recovery dispositions")
			}
			accepted = outcome.action
		} else if !errors.Is(outcome.err, ErrConflict) {
			t.Fatal(outcome.err)
		}
	}
	desired, err := manager.store.readDesired("machine-1")
	if err != nil || accepted == "" || desired.Recovery.Action != accepted {
		t.Fatalf("accepted %q, persisted %+v, error %v", accepted, desired.Recovery, err)
	}
}
