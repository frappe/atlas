package firecracker

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestOperationLocksSerializeOneVM(t *testing.T) {
	var locks operationLocks
	unlockFirst, err := locks.lock(context.Background(), "vm-1")
	if err != nil {
		t.Fatal(err)
	}
	acquired := make(chan struct{})

	go func() {
		unlockSecond, lockError := locks.lock(context.Background(), "vm-1")
		if lockError != nil {
			return
		}
		close(acquired)
		unlockSecond()
	}()

	select {
	case <-acquired:
		t.Fatal("second operation acquired the VM lock early")
	case <-time.After(20 * time.Millisecond):
	}
	unlockFirst()

	select {
	case <-acquired:
	case <-time.After(time.Second):
		t.Fatal("second operation did not acquire the VM lock")
	}
}

func TestOperationLocksAllowDifferentVMs(t *testing.T) {
	var locks operationLocks
	unlockFirst, err := locks.lock(context.Background(), "vm-1")
	if err != nil {
		t.Fatal(err)
	}
	defer unlockFirst()

	unlockSecond, err := locks.lock(context.Background(), "vm-2")
	if err != nil {
		t.Fatal(err)
	}
	unlockSecond()
}

func TestOperationLockWaitHonorsCancellation(t *testing.T) {
	var locks operationLocks
	unlock, err := locks.lock(context.Background(), "vm-1")
	if err != nil {
		t.Fatal(err)
	}
	defer unlock()

	operationContext, cancel := context.WithCancel(context.Background())
	cancel()
	_, err = locks.lock(operationContext, "vm-1")
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("lock error = %v, want context cancellation", err)
	}
}
