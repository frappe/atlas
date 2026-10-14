// Package telemetry records metald and host metrics and pushes them to Datum.
package telemetry

import (
	"context"
	"log/slog"
	"maps"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"
)

// Bound memory use during a Datum outage.
const maximumObservations = 50_000

const collectorTimeout = 10 * time.Second

var (
	metricName = regexp.MustCompile(`^[a-z_][a-z0-9_]{0,199}$`)
	labelName  = regexp.MustCompile(`^[a-z_][a-z0-9_]*$`)
)

// Datum retains series forever. Put resource and request IDs in logs.
var churningLabels = []string{"virtual_machine", "virtual_machine_id", "request_id", "operation_id", "migration_id", "pid", "uuid"}

// Labels name one series of a metric.
type Labels map[string]string

// Sample is one Datum reading.
type Sample struct {
	Metric string    `json:"metric"`
	Value  float64   `json:"value"`
	Time   time.Time `json:"ts"`
	Labels Labels    `json:"labels,omitempty"`
}

// Collector reads current values when the registry takes a snapshot.
type Collector interface {
	Collect(ctx context.Context) ([]Sample, error)
}

// CollectorFunc adapts a function to Collector.
type CollectorFunc func(ctx context.Context) ([]Sample, error)

// Collect calls function.
func (function CollectorFunc) Collect(ctx context.Context) ([]Sample, error) {
	return function(ctx)
}

type counter struct {
	metric string
	labels Labels
	value  float64
}

type namedCollector struct {
	name      string
	collector Collector
	failing   bool
}

// Registry owns counters, last-value gauges, pending observations, and collectors.
type Registry struct {
	logger *slog.Logger

	mutex        sync.Mutex
	counters     map[string]*counter
	gauges       map[string]*counter
	observations []Sample
	collectors   []*namedCollector
	invalid      map[string]bool
}

// NewRegistry returns an empty registry.
func NewRegistry(logger *slog.Logger) *Registry {
	if logger == nil {
		logger = slog.Default()
	}
	return &Registry{
		logger:   logger,
		counters: make(map[string]*counter),
		gauges:   make(map[string]*counter),
		invalid:  make(map[string]bool),
	}
}

// Count adds value to a cumulative counter. Its name ends in _total.
func (registry *Registry) Count(metric string, value float64, labels Labels) {
	registry.mutex.Lock()
	defer registry.mutex.Unlock()

	if !registry.isValid(metric, labels) {
		return
	}
	key := seriesKey(metric, labels)
	entry, ok := registry.counters[key]
	if !ok {
		entry = &counter{metric: metric, labels: maps.Clone(labels)}
		registry.counters[key] = entry
	}
	entry.value += value
}

// Set stores the latest value of a gauge, such as the time of the last pass.
func (registry *Registry) Set(metric string, value float64, labels Labels) {
	registry.mutex.Lock()
	defer registry.mutex.Unlock()

	if !registry.isValid(metric, labels) {
		return
	}
	registry.gauges[seriesKey(metric, labels)] = &counter{metric: metric, labels: maps.Clone(labels), value: value}
}

// Observe records an event value for exact percentiles.
func (registry *Registry) Observe(metric string, value float64, labels Labels) {
	registry.mutex.Lock()
	defer registry.mutex.Unlock()

	if !registry.isValid(metric, labels) {
		return
	}
	registry.observations = append(registry.observations, Sample{Metric: metric, Value: value, Time: time.Now().UTC(), Labels: maps.Clone(labels)})
	registry.dropOldestObservations()
}

// Register adds a collector that runs on every snapshot.
func (registry *Registry) Register(name string, collector Collector) {
	registry.mutex.Lock()
	defer registry.mutex.Unlock()
	registry.collectors = append(registry.collectors, &namedCollector{name: name, collector: collector})
}

// Snapshot collects current metrics, retaining partial results on collector errors.
func (registry *Registry) Snapshot(ctx context.Context, now time.Time) []Sample {
	registry.mutex.Lock()
	collectors := slices.Clone(registry.collectors)
	registry.mutex.Unlock()

	var samples []Sample
	for _, entry := range collectors {
		collectContext, cancel := context.WithTimeout(ctx, collectorTimeout)
		collected, err := entry.collector.Collect(collectContext)
		cancel()
		registry.reportCollector(entry, err)
		samples = append(samples, collected...)
	}

	registry.mutex.Lock()
	defer registry.mutex.Unlock()

	valid := samples[:0]
	for _, sample := range samples {
		if !registry.isValid(sample.Metric, sample.Labels) {
			continue
		}
		if sample.Time.IsZero() {
			sample.Time = now
		}
		valid = append(valid, sample)
	}
	for _, series := range []map[string]*counter{registry.counters, registry.gauges} {
		for _, entry := range series {
			valid = append(valid, Sample{Metric: entry.metric, Value: entry.value, Time: now, Labels: entry.labels})
		}
	}
	return valid
}

// TakeObservations removes and returns the pending observations.
func (registry *Registry) TakeObservations() []Sample {
	registry.mutex.Lock()
	defer registry.mutex.Unlock()

	observations := registry.observations
	registry.observations = nil
	return observations
}

// RestoreObservations puts back observations that Datum did not accept.
func (registry *Registry) RestoreObservations(observations []Sample) {
	registry.mutex.Lock()
	defer registry.mutex.Unlock()

	// They are older than the ones recorded during the failed push, so they go
	// first and are dropped first.
	registry.observations = slices.Concat(observations, registry.observations)
	registry.dropOldestObservations()
}

// dropOldestObservations keeps the newest observations within the bound and
// counts the dropped ones. The caller holds the mutex.
func (registry *Registry) dropOldestObservations() {
	overflow := len(registry.observations) - maximumObservations
	if overflow <= 0 {
		return
	}

	registry.observations = slices.Delete(registry.observations, 0, overflow)
	key := seriesKey(droppedObservationsMetric, nil)
	entry, ok := registry.counters[key]
	if !ok {
		entry = &counter{metric: droppedObservationsMetric}
		registry.counters[key] = entry
	}
	entry.value += float64(overflow)
}

// reportCollector logs a collector failure and its recovery once each.
func (registry *Registry) reportCollector(entry *namedCollector, err error) {
	registry.mutex.Lock()
	wasFailing := entry.failing
	entry.failing = err != nil
	registry.mutex.Unlock()

	switch {
	case err != nil && !wasFailing:
		registry.logger.Warn("telemetry collector failed", "collector", entry.name, "error", err)
	case err == nil && wasFailing:
		registry.logger.Info("telemetry collector recovered", "collector", entry.name)
	}
	if err != nil {
		registry.Count(collectorErrorsMetric, 1, Labels{"collector": entry.name})
	}
}

// The caller holds the mutex. Invalid metric names are logged once.
func (registry *Registry) isValid(metric string, labels Labels) bool {
	reason := ""
	switch {
	case !metricName.MatchString(metric):
		reason = "invalid metric name"
	default:
		for name := range labels {
			if !labelName.MatchString(name) || slices.Contains(churningLabels, name) {
				reason = "invalid or churning label " + name
				break
			}
		}
	}
	if reason == "" {
		return true
	}

	if !registry.invalid[metric] {
		registry.invalid[metric] = true
		registry.logger.Error("telemetry sample rejected", "metric", metric, "reason", reason)
	}
	return false
}

// seriesKey identifies one counter series independent of label order.
func seriesKey(metric string, labels Labels) string {
	var key strings.Builder
	key.WriteString(metric)
	for _, name := range slices.Sorted(maps.Keys(labels)) {
		key.WriteString("\x00")
		key.WriteString(name)
		key.WriteString("=")
		key.WriteString(labels[name])
	}
	return key.String()
}
