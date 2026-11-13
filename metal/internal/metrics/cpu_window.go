package metrics

import (
	"sync"
	"time"
)

// CPUWindow keeps the recent samples of each virtual machine in memory, so a
// frequent CPU check does not decode the day files.
type CPUWindow struct {
	length  time.Duration
	mutex   sync.Mutex
	records map[string][]Record
}

// NewCPUWindow returns a window that keeps samples for length.
func NewCPUWindow(length time.Duration) *CPUWindow {
	return &CPUWindow{length: length, records: make(map[string][]Record)}
}

// Length returns the time that the window covers.
func (window *CPUWindow) Length() time.Duration {
	return window.length
}

// Add appends a record that is newer than the previous one and drops the
// records that fall out of the window.
func (window *CPUWindow) Add(identifier string, record Record) {
	window.mutex.Lock()
	defer window.mutex.Unlock()

	records := append(window.records[identifier], record)
	window.records[identifier] = records[firstInWindow(records, record.Timestamp.Add(-window.length)):]
}

// Usage returns the CPU use of one virtual machine in the samples from start.
func (window *CPUWindow) Usage(identifier string, start time.Time) CPUUsage {
	window.mutex.Lock()
	defer window.mutex.Unlock()

	records := window.records[identifier]
	return MeasureCPUUsage(records[firstInWindow(records, start):])
}

// Retain drops the virtual machines that are not active.
func (window *CPUWindow) Retain(active map[string]bool) {
	window.mutex.Lock()
	defer window.mutex.Unlock()

	for identifier := range window.records {
		if !active[identifier] {
			delete(window.records, identifier)
		}
	}
}

func firstInWindow(records []Record, start time.Time) int {
	first := 0
	for first < len(records) && records[first].Timestamp.Before(start) {
		first++
	}
	return first
}
