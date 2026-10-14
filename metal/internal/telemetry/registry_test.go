package telemetry

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"
)

func newTestRegistry() *Registry {
	return NewRegistry(slog.New(slog.NewTextHandler(io.Discard, nil)))
}

func findSample(samples []Sample, metric string, labels Labels) (Sample, bool) {
	for _, sample := range samples {
		if sample.Metric == metric && seriesKey(metric, sample.Labels) == seriesKey(metric, labels) {
			return sample, true
		}
	}
	return Sample{}, false
}

func TestRegistryRejectsLabelsThatAddASeriesPerVM(t *testing.T) {
	registry := newTestRegistry()
	registry.Count("metal_vm_operations_total", 1, Labels{"virtual_machine_id": "vm-1-0001"})
	registry.Count("metal_vm_operations_total", 1, Labels{"phase": "start"})

	samples := registry.Snapshot(context.Background(), time.Now())
	if len(samples) != 1 || samples[0].Labels["phase"] != "start" {
		t.Fatalf("samples = %+v, want only the phase series", samples)
	}
}

func TestRegistryAddsCountsOfOneSeriesInAnyLabelOrder(t *testing.T) {
	registry := newTestRegistry()
	registry.Count("metal_api_requests_total", 1, Labels{"route": "/v1/vms/:id", "status": "200"})
	registry.Count("metal_api_requests_total", 2, Labels{"status": "200", "route": "/v1/vms/:id"})

	sample, ok := findSample(registry.Snapshot(context.Background(), time.Now()), "metal_api_requests_total", Labels{"route": "/v1/vms/:id", "status": "200"})
	if !ok || sample.Value != 3 {
		t.Fatalf("sample = %+v, want 3", sample)
	}
}

func TestRegistryKeepsNewestObservationsWithinTheBound(t *testing.T) {
	registry := newTestRegistry()
	for index := range maximumObservations + 10 {
		registry.Observe("metal_vm_action_duration_seconds", float64(index), nil)
	}

	observations := registry.TakeObservations()
	if len(observations) != maximumObservations || observations[0].Value != 10 {
		t.Fatalf("kept %d observations starting at %v, want %d starting at 10", len(observations), observations[0].Value, maximumObservations)
	}
	dropped, ok := findSample(registry.Snapshot(context.Background(), time.Now()), droppedObservationsMetric, nil)
	if !ok || dropped.Value != 10 {
		t.Fatalf("dropped = %+v, want 10", dropped)
	}
}

func TestRegistryReportsOtherCollectorsWhenOneFails(t *testing.T) {
	registry := newTestRegistry()
	registry.Register("broken", CollectorFunc(func(context.Context) ([]Sample, error) {
		return nil, errors.New("read failed")
	}))
	registry.Register("working", CollectorFunc(func(context.Context) ([]Sample, error) {
		return []Sample{{Metric: "system_cpu_count", Value: 8}}, nil
	}))

	now := time.Now()
	samples := registry.Snapshot(context.Background(), now)
	cpuCount, ok := findSample(samples, "system_cpu_count", nil)
	if !ok || cpuCount.Value != 8 || !cpuCount.Time.Equal(now) {
		t.Fatalf("cpu count = %+v, want 8 at the snapshot time", cpuCount)
	}
	if _, ok := findSample(registry.Snapshot(context.Background(), now), collectorErrorsMetric, Labels{"collector": "broken"}); !ok {
		t.Fatal("collector failure was not counted")
	}
}

func TestRegistryGivesEachCollectorADeadline(t *testing.T) {
	registry := newTestRegistry()
	var deadline time.Time
	registry.Register("smart", CollectorFunc(func(ctx context.Context) ([]Sample, error) {
		deadline, _ = ctx.Deadline()
		return nil, nil
	}))

	registry.Snapshot(context.Background(), time.Now())
	if deadline.IsZero() || time.Until(deadline) > collectorTimeout {
		t.Fatalf("deadline = %v, want one within %v so a hung command cannot stop the push", deadline, collectorTimeout)
	}
}

func TestRegistryKeepsNewObservationsOverRestoredOnes(t *testing.T) {
	registry := newTestRegistry()
	for index := range maximumObservations {
		registry.Observe("metal_vm_action_duration_seconds", float64(index), nil)
	}
	failed := registry.TakeObservations()
	registry.Observe("metal_vm_action_duration_seconds", -1, nil)

	registry.RestoreObservations(failed)

	kept := registry.TakeObservations()
	if len(kept) != maximumObservations || kept[0].Value != 1 || kept[len(kept)-1].Value != -1 {
		t.Fatalf("kept %d observations from %v to %v, want the oldest restored one dropped and the new one kept", len(kept), kept[0].Value, kept[len(kept)-1].Value)
	}
}
