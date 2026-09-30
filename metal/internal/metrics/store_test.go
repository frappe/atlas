package metrics

import (
	"context"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/vm"
)

func TestRetentionAndRestart(t *testing.T) {
	ctx := context.Background()
	directory := t.TempDir()
	store, err := NewStore(directory)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 9, 30, 12, 30, 0, 0, time.UTC)
	cutoff := now.Add(-Retention)
	for _, offset := range []time.Duration{-time.Hour, -time.Second, 0, time.Second, 23 * time.Hour} {
		usage := vm.Metrics{MemoryBytes: 42}
		if err := store.Append("vm:one", Record{Timestamp: cutoff.Add(offset), VM: &usage}); err != nil {
			t.Fatal(err)
		}
	}
	if err := store.Append("vm:deleted", Record{Timestamp: cutoff.Add(-time.Hour), VM: &vm.Metrics{}}); err != nil {
		t.Fatal(err)
	}
	if err := store.Prune(ctx, now); err != nil {
		t.Fatal(err)
	}
	reopened, err := NewStore(directory)
	if err != nil {
		t.Fatal(err)
	}
	records, err := reopened.History(ctx, "vm:one", time.Time{}, now)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 3 || !records[0].Timestamp.Equal(cutoff) || records[0].VM.MemoryBytes != 42 {
		t.Fatalf("records=%+v", records)
	}
	if _, err := os.Stat(store.resourceDirectory("vm:deleted")); !os.IsNotExist(err) {
		t.Fatalf("expired directory: %v", err)
	}
	records, err = reopened.History(ctx, "vm:one", cutoff, cutoff.Add(time.Second))
	if err != nil || len(records) != 1 {
		t.Fatalf("range=%+v, %v", records, err)
	}
	records, err = reopened.History(ctx, "vm:other", time.Time{}, now)
	if err != nil || len(records) != 0 {
		t.Fatalf("resource isolation: %+v, %v", records, err)
	}
}

func TestInterruptedAppendAndCorruption(t *testing.T) {
	store, err := NewStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC().Truncate(time.Second)
	record := Record{Timestamp: now, VM: &vm.Metrics{MemoryBytes: 1}}
	if err := store.Append("vm:one", record); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(store.resourceDirectory("vm:one"), now.Format(hourLayout)+".jsonl")
	file, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.WriteString(`{"timestamp":`); err != nil {
		t.Fatal(err)
	}
	file.Close()
	records, err := store.History(context.Background(), "vm:one", now.Add(-time.Second), now.Add(time.Second))
	if err != nil || len(records) != 1 {
		t.Fatalf("interrupted read: %+v, %v", records, err)
	}
	if err := store.Append("vm:one", record); err != nil {
		t.Fatal(err)
	}
	records, err = store.History(context.Background(), "vm:one", now.Add(-time.Second), now.Add(time.Second))
	if err != nil || len(records) != 2 {
		t.Fatalf("recovered append: %+v, %v", records, err)
	}
	if err := os.WriteFile(path, []byte("broken\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := store.History(context.Background(), "vm:one", now.Add(-time.Second), now.Add(time.Second)); err == nil {
		t.Fatal("corrupt complete record accepted")
	}
}

func TestConcurrentAccess(t *testing.T) {
	store, err := NewStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	var workers sync.WaitGroup
	for range 4 {
		workers.Go(func() {
			for range 10 {
				if err := store.Append("vm:one", Record{Timestamp: now, VM: &vm.Metrics{}}); err != nil {
					t.Error(err)
				}
				if _, err := store.History(context.Background(), "vm:one", now.Add(-time.Hour), now.Add(time.Hour)); err != nil {
					t.Error(err)
				}
				if err := store.Prune(context.Background(), now); err != nil {
					t.Error(err)
				}
			}
		})
	}
	workers.Wait()
	records, err := store.History(context.Background(), "vm:one", now.Add(-time.Hour), now.Add(time.Hour))
	if err != nil || len(records) != 40 {
		t.Fatalf("records=%d, %v", len(records), err)
	}
}
