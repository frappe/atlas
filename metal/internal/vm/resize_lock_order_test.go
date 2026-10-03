package vm

import (
	"context"
	"errors"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/network/traffic"
)

const resizeWatchdog = 10 * time.Second

const operationLockPollDeadline = 5 * time.Second

func ampleResizeCapacity(context.Context) (ResizeCapacity, error) {
	return ResizeCapacity{AvailableMemoryMiB: 1 << 30, AvailableStorageMiB: 1 << 30}, nil
}

func resizeLikeHandler(ctx context.Context, manager *Manager, identifier string, compute Compute, diskMiB int) error {
	return manager.ResizeWithCapacity(ctx, identifier, compute, diskMiB, ampleResizeCapacity)
}

func stopTestVirtualMachine(t *testing.T, manager *Manager, identifier string) {
	t.Helper()
	if _, err := manager.Create(context.Background(), identifier, testSpecification()); err != nil {
		t.Fatal(err)
	}
	if err := manager.SetPowerState(context.Background(), identifier, StateStopped); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(context.Background(), identifier); err != nil {
		t.Fatal(err)
	}
	observed, err := manager.store.readObserved(identifier)
	if err != nil {
		t.Fatal(err)
	}
	if observed.State != StateStopped {
		t.Fatalf("observed state = %s, want stopped", observed.State)
	}
}

func TestResizeSetNetworkLockOrder(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	const identifier = "machine-1"
	stopTestVirtualMachine(t, manager, identifier)

	resizeCompute := Compute{CPUMillicores: 2000, MemoryMiB: 4096}
	const resizeDiskMiB = 8192
	setNetwork := testSpecification().Network
	setNetwork.PublicNetworkThroughputMiBps = 500

	resizeContext, cancelResize := context.WithCancel(context.Background())
	networkContext, cancelNetwork := context.WithCancel(context.Background())
	defer cancelResize()
	defer cancelNetwork()

	resizeDone := make(chan error, 1)
	networkDone := make(chan error, 1)

	// Hold allocation while resize acquires the operation lock.
	manager.allocationMutex.Lock()

	go func() {
		resizeDone <- resizeLikeHandler(resizeContext, manager, identifier, resizeCompute, resizeDiskMiB)
	}()
	// References include waiters registered before they block.
	waitOperationLockReferences(t, manager, identifier, 1, resizeDone, networkDone)

	// Allocation stays locked, so the second reference proves SetNetwork is waiting.
	go func() {
		networkDone <- manager.SetNetwork(networkContext, identifier, setNetwork)
	}()
	waitOperationLockReferences(t, manager, identifier, 2, resizeDone, networkDone)
	assertNoResult(t, resizeDone, networkDone)

	manager.allocationMutex.Unlock()

	deadline := time.After(resizeWatchdog)
	var resizeErr, networkErr error
	resizeFinished, networkFinished := false, false
	for !resizeFinished || !networkFinished {
		select {
		case err := <-resizeDone:
			resizeErr, resizeFinished = err, true
		case err := <-networkDone:
			networkErr, networkFinished = err, true
		case <-deadline:
			cancelResize()
			cancelNetwork()
			<-resizeDone
			<-networkDone
			t.Fatal("resize and SetNetwork did not both finish: lock order regressed")
		}
	}
	if resizeErr != nil {
		t.Fatalf("resize error = %v, want nil", resizeErr)
	}
	if networkErr != nil {
		t.Fatalf("SetNetwork error = %v, want nil", networkErr)
	}
	if err := resizeContext.Err(); err != nil {
		t.Fatalf("resize context = %v, want no cancellation", err)
	}
	if err := networkContext.Err(); err != nil {
		t.Fatalf("network context = %v, want no cancellation", err)
	}

	record, err := manager.store.readDesired(identifier)
	if err != nil {
		t.Fatal(err)
	}
	if record.Specification.CPUMillicores != resizeCompute.CPUMillicores ||
		record.Specification.MemoryMiB != resizeCompute.MemoryMiB ||
		record.Specification.DiskMiB != resizeDiskMiB {
		t.Fatalf("resized shape = %+v, want compute %+v disk %d",
			record.Specification, resizeCompute, resizeDiskMiB)
	}
	if !setNetwork.Equal(record.Specification.Network) {
		t.Fatalf("network = %+v, want %+v", record.Specification.Network, setNetwork)
	}
}

// waitOperationLockReferences observes contention without acquiring the operation lock.
func waitOperationLockReferences(t *testing.T, manager *Manager, identifier string, want int, resizeDone, networkDone <-chan error) {
	t.Helper()
	deadline := time.Now().Add(operationLockPollDeadline)
	for time.Now().Before(deadline) {
		assertNoResult(t, resizeDone, networkDone)
		if operationLockReferences(manager, identifier) >= want {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("operation lock references did not reach %d; test setup is wrong", want)
}

func operationLockReferences(manager *Manager, identifier string) int {
	manager.operationLocks.mutex.Lock()
	defer manager.operationLocks.mutex.Unlock()
	entry := manager.operationLocks.locks[identifier]
	if entry == nil {
		return 0
	}
	return entry.references
}

func assertNoResult(t *testing.T, resizeDone, networkDone <-chan error) {
	t.Helper()
	select {
	case err := <-resizeDone:
		t.Fatalf("resize returned %v before the overlap completed; test setup is wrong", err)
	default:
	}
	select {
	case err := <-networkDone:
		t.Fatalf("SetNetwork returned %v before the overlap completed; test setup is wrong", err)
	default:
	}
}

func TestResizeCapacityAdmitsOneWinner(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	stopTestVirtualMachine(t, manager, "machine-a")
	stopTestVirtualMachine(t, manager, "machine-b")

	const (
		memoryBudgetMiB  = 4608
		storageBudgetMiB = 9216
	)
	budgetCapacity := func(context.Context) (ResizeCapacity, error) {
		identifiers, err := manager.store.listIDs()
		if err != nil {
			return ResizeCapacity{}, err
		}
		reservedMemoryMiB, reservedStorageMiB := 0, 0
		for _, identifier := range identifiers {
			record, err := manager.store.readDesired(identifier)
			if err != nil {
				return ResizeCapacity{}, err
			}
			reservedMemoryMiB += record.Specification.MemoryMiB
			reservedStorageMiB += record.Specification.DiskMiB
		}
		return ResizeCapacity{
			AvailableMemoryMiB:  memoryBudgetMiB - reservedMemoryMiB,
			AvailableStorageMiB: storageBudgetMiB - reservedStorageMiB,
		}, nil
	}

	const (
		targetMemoryMiB = 2560
		targetDiskMiB   = 4608
	)
	start := make(chan struct{})
	results := make(chan error, 2)
	var group sync.WaitGroup
	for _, identifier := range []string{"machine-a", "machine-b"} {
		group.Add(1)
		go func() {
			defer group.Done()
			<-start
			results <- manager.ResizeWithCapacity(context.Background(), identifier,
				Compute{CPUMillicores: 2000, MemoryMiB: targetMemoryMiB}, targetDiskMiB, budgetCapacity)
		}()
	}
	close(start)
	group.Wait()
	close(results)

	winners, losers := 0, 0
	for err := range results {
		switch {
		case err == nil:
			winners++
		case errors.Is(err, ErrInsufficientCapacity):
			losers++
		default:
			t.Fatalf("resize error = %v, want nil or insufficient capacity", err)
		}
	}
	if winners != 1 || losers != 1 {
		t.Fatalf("winners = %d, losers = %d, want exactly one of each", winners, losers)
	}
}

func setObservedDiskSize(t *testing.T, manager *Manager, identifier string, sizeMiB int) {
	t.Helper()
	observed, err := manager.store.readObserved(identifier)
	if err != nil {
		t.Fatal(err)
	}
	observed.Disk.SizeMiB = sizeMiB
	if err := manager.store.writeObserved(identifier, observed); err != nil {
		t.Fatal(err)
	}
}

func fixedResizeCapacity(freeMemoryMiB, freeStorageMiB int) ResizeCapacitySource {
	return func(context.Context) (ResizeCapacity, error) {
		return ResizeCapacity{AvailableMemoryMiB: freeMemoryMiB, AvailableStorageMiB: freeStorageMiB}, nil
	}
}

func TestResizeDiskBaseUsesObservedSize(t *testing.T) {
	manager, _, _, _ := newTestManager(t)

	stopTestVirtualMachine(t, manager, "disk-grow-lag")
	setObservedDiskSize(t, manager, "disk-grow-lag", 2048)
	specification, err := manager.store.readDesired("disk-grow-lag")
	if err != nil {
		t.Fatal(err)
	}
	if specification.Specification.DiskMiB != 4096 {
		t.Fatalf("spec disk = %d, want 4096", specification.Specification.DiskMiB)
	}
	err = manager.ResizeWithCapacity(context.Background(), "disk-grow-lag",
		Compute{CPUMillicores: 2000, MemoryMiB: 2048}, 6144, fixedResizeCapacity(1<<30, 2048))
	if !errors.Is(err, ErrInsufficientCapacity) {
		t.Fatalf("grow-lag resize error = %v, want insufficient capacity", err)
	}

	stopTestVirtualMachine(t, manager, "disk-shrink-lag")
	setObservedDiskSize(t, manager, "disk-shrink-lag", 6144)
	err = manager.ResizeWithCapacity(context.Background(), "disk-shrink-lag",
		Compute{CPUMillicores: 2000, MemoryMiB: 2048}, 7168, fixedResizeCapacity(1<<30, 2048))
	if err != nil {
		t.Fatalf("shrink-lag resize error = %v, want nil", err)
	}
}

func TestResizeMissingObservedFailsInsteadOfZeroBase(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	const identifier = "machine-1"
	stopTestVirtualMachine(t, manager, identifier)
	if err := os.Remove(manager.store.observedPath(identifier)); err != nil {
		t.Fatal(err)
	}
	err := manager.ResizeWithCapacity(context.Background(), identifier,
		Compute{CPUMillicores: 2000, MemoryMiB: 4096}, 8192, ampleResizeCapacity)
	if err == nil {
		t.Fatal("resize with missing observed record succeeded, want an error")
	}
	if errors.Is(err, ErrInsufficientCapacity) {
		t.Fatalf("resize error = %v, want the read error, not insufficient capacity", err)
	}
	if !errors.Is(err, ErrNotFound) {
		t.Fatalf("resize error = %v, want the wrapped not-found read error", err)
	}
}

func TestResizeCapacityParityMatrix(t *testing.T) {
	// The matrix needs more VM user IDs than newTestManager provides.
	manager, err := NewManager(
		ManagerConfig{MachinesDirectory: t.TempDir(), UserIDRange: UserIDRange{Min: 1000, Max: 1100}},
		ManagerDependencies{
			Runtime:   &fakeRuntime{state: StateStopped},
			Network:   &fakeNetwork{},
			Storage:   &fakeStorage{},
			Snapshots: fakeSnapshots{},
			Traffic:   &traffic.Monitor{},
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name             string
		volSizeMiB       int
		specDiskMiB      int
		targetDiskMiB    int
		freeStorageMiB   int
		wantInsufficient bool
		wantConflict     bool
	}{
		{"fallback-admits", 0, 4096, 6144, 2048, false, false},
		{"fallback-rejects", 0, 4096, 7168, 2048, true, false},
		{"grow-lag-rejects", 2048, 4096, 6144, 2048, true, false},
		{"no-change-rejects", 2048, 4096, 4096, 0, true, false},
		{"grow-lag-exact-fit", 2048, 4096, 6144, 4096, false, false},
		{"steady-admits", 4096, 4096, 6144, 2048, false, false},
		{"steady-rejects", 4096, 4096, 7168, 2048, true, false},
		{"reverse-lag-admits", 6144, 4096, 7168, 2048, false, false},
		{"reverse-lag-rejects", 6144, 4096, 9216, 2048, true, false},
		{"reverse-lag-no-growth", 6144, 4096, 6144, 0, false, false},
		{"vol-above-target", 8192, 4096, 6144, 10240, false, false},
		{"shrink-conflicts", 2048, 4096, 3072, 10240, false, true},
		{"shrink-from-lag-conflicts", 6144, 8192, 7168, 10240, false, true},
		{"fallback-no-change", 0, 4096, 4096, 0, false, false},
		{"off-steady-exact-fit", 3072, 4096, 6144, 3072, false, false},
		{"off-steady-rejects", 3072, 4096, 6144, 2048, true, false},
	}
	for index, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			identifier := string(rune('a'+index/26)) + string(rune('a'+index%26))
			specification := testSpecification()
			specification.DiskMiB = testCase.specDiskMiB
			if _, err := manager.Create(context.Background(), identifier, specification); err != nil {
				t.Fatal(err)
			}
			if err := manager.SetPowerState(context.Background(), identifier, StateStopped); err != nil {
				t.Fatal(err)
			}
			if err := manager.Reconcile(context.Background(), identifier); err != nil {
				t.Fatal(err)
			}
			setObservedDiskSize(t, manager, identifier, testCase.volSizeMiB)
			err := manager.ResizeWithCapacity(context.Background(), identifier,
				Compute{CPUMillicores: specification.CPUMillicores, MemoryMiB: specification.MemoryMiB},
				testCase.targetDiskMiB, fixedResizeCapacity(1<<30, testCase.freeStorageMiB))
			switch {
			case testCase.wantInsufficient:
				if !errors.Is(err, ErrInsufficientCapacity) {
					t.Fatalf("error = %v, want insufficient capacity", err)
				}
			case testCase.wantConflict:
				if !errors.Is(err, ErrConflict) {
					t.Fatalf("error = %v, want conflict", err)
				}
			default:
				if err != nil {
					t.Fatalf("error = %v, want nil", err)
				}
			}
		})
	}
}
