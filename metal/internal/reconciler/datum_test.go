package reconciler

import (
	"context"
	"sync"
	"testing"
	"time"
)

type recordingExporter struct {
	mu    sync.Mutex
	calls int
	done  chan struct{}
}

func newRecordingExporter() *recordingExporter {
	return &recordingExporter{done: make(chan struct{}, 16)}
}

func (exporter *recordingExporter) PushAll(context.Context) {
	exporter.mu.Lock()
	exporter.calls++
	exporter.mu.Unlock()
	exporter.done <- struct{}{}
}

func (exporter *recordingExporter) count() int {
	exporter.mu.Lock()
	defer exporter.mu.Unlock()
	return exporter.calls
}

func TestDatumReconcilerRunsOnStartupAndOnWake(t *testing.T) {
	exporter := newRecordingExporter()
	reconciler := NewDatumReconciler(exporter, time.Hour)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go reconciler.Run(ctx)

	select {
	case <-exporter.done:
	case <-time.After(time.Second):
		t.Fatal("expected a pass at startup")
	}

	reconciler.Wake()
	select {
	case <-exporter.done:
	case <-time.After(time.Second):
		t.Fatal("expected a pass after Wake")
	}

	if exporter.count() != 2 {
		t.Fatalf("calls = %d, want 2", exporter.count())
	}
}
