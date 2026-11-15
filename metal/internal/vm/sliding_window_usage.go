package vm

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/frappe/atlas/metal/internal/metrics"
)

// SlidingWindowUsage is a VM's average resource use over the manager's sliding window.
type SlidingWindowUsage struct {
	// MilliCPUs is average CPU use in thousandths of a CPU. 1500 means 1.5 CPUs.
	MilliCPUs uint64
}

// slidingWindowUsage keeps the newest CPU samples of each VM in memory, so a
// read does not decode the daily metrics file. The sampler records each stored
// sample, and the store fills a VM's ring once per metald run.
//
//	event                 call                 ring of vm1               isSeeded
//	metald starts         -                    (none)                    -
//	sweep stores t10      record(vm1, t10)     [t10]                     false
//	first read            History, seed        [t-20 t-10 t0 t10]        true
//	sweep stores t20      record(vm1, t20)     [t-10 t0 t10 t20]         true
//	later reads           average only         (no disk read)            true
//	destroy or migrate    forget(vm1)          (none)                    -
type slidingWindowUsage struct {
	window time.Duration

	// capacity is window / metrics.SampleInterval + 1, because N intervals need N + 1 samples.
	capacity int

	// mutex guards rings and every ring in it. It is never held during a disk read.
	mutex sync.Mutex
	rings map[string]*cpuSampleRing
}

type cpuSample struct {
	// timestamp has whole seconds, like the store, so a recorded sample equals its stored copy.
	timestamp       time.Time
	isUp            bool
	cpuMicroseconds uint64
}

// cpuSampleRing is a fixed-size ring that overwrites its oldest sample.
//
//	capacity 4, after t10 t20 t30         after t40 t50 (t50 overwrites t10)
//	slot   0     1     2     3            slot   0     1     2     3
//	     [t10] [t20] [t30] [   ]               [t50] [t20] [t30] [t40]
//	                        ^next                    ^next
//	count 3                               count 4, oldest = slot 1 = at(0)
type cpuSampleRing struct {
	samples []cpuSample
	next    int
	count   int

	// isSeeded is true after the ring was filled from the store in this metald run.
	isSeeded bool
}

func newSlidingWindowUsage(window time.Duration) *slidingWindowUsage {
	return &slidingWindowUsage{
		window:   window,
		capacity: int(window/metrics.SampleInterval) + 1,
		rings:    make(map[string]*cpuSampleRing),
	}
}

func newCPUSample(record metrics.Record) cpuSample {
	return cpuSample{
		timestamp:       record.Timestamp.Truncate(time.Second),
		isUp:            record.Metrics.Up,
		cpuMicroseconds: record.Metrics.CPUTimeMicroseconds,
	}
}

func (usage *slidingWindowUsage) newRing() *cpuSampleRing {
	return &cpuSampleRing{samples: make([]cpuSample, usage.capacity)}
}

func (ring *cpuSampleRing) push(sample cpuSample) {
	ring.samples[ring.next] = sample
	ring.next = (ring.next + 1) % len(ring.samples)
	ring.count = min(ring.count+1, len(ring.samples))
}

// at returns the sample at position index, where 0 is the oldest.
func (ring *cpuSampleRing) at(index int) cpuSample {
	return ring.samples[(ring.next-ring.count+index+len(ring.samples))%len(ring.samples)]
}

func (usage *slidingWindowUsage) record(identifier string, sample cpuSample) {
	usage.mutex.Lock()
	defer usage.mutex.Unlock()

	ring, found := usage.rings[identifier]
	if !found {
		ring = usage.newRing()
		usage.rings[identifier] = ring
	}
	ring.push(sample)
}

func (usage *slidingWindowUsage) isSeeded(identifier string) bool {
	usage.mutex.Lock()
	defer usage.mutex.Unlock()

	ring, found := usage.rings[identifier]
	return found && ring.isSeeded
}

// seed fills the VM's ring with stored samples, then keeps the recorded samples
// that are newer than the last stored one. A sweep can record a sample while
// History reads the file.
//
//	case                         stored               ring before      ring after (capacity 4)
//	normal restart               t10 t20 t30          [t30]            [t10 t20 t30]
//	sweep during History read    t10 t20 t30          [t30 t40]        [t10 t20 t30 t40]
//	more stored than capacity    t10 t20 t30 t40 t50  [t50]            [t20 t30 t40 t50]
//	new VM, no history yet       (none)               [t10]            [t10]
//	unknown VM                   (none)               (none)           (none)
func (usage *slidingWindowUsage) seed(identifier string, stored []cpuSample) {
	usage.mutex.Lock()
	defer usage.mutex.Unlock()

	recorded, found := usage.rings[identifier]
	if !found && len(stored) == 0 {
		return
	}

	ring := usage.newRing()
	for _, sample := range stored {
		ring.push(sample)
	}
	if found {
		for index := range recorded.count {
			sample := recorded.at(index)
			if len(stored) == 0 || sample.timestamp.After(stored[len(stored)-1].timestamp) {
				ring.push(sample)
			}
		}
	}
	ring.isSeeded = true
	usage.rings[identifier] = ring
}

// average returns the mean CPU use of the pairs of adjacent samples inside the window.
//
//	MilliCPUs = sum(CPU used in each pair) * 1000 / sum(elapsed time of each pair)
//
// Example: window 30 s, now t60. t20 is outside the window.
//
//	time      t20 | t30    t40    t50    t60
//	up        yes | yes    no     yes    yes
//	              |   \___/  \___/  \___/
//	pair          |   skip   skip   use       a down sample skips both of its pairs
//
//	same second      t30 -> t30         skipped
//	counter falls    cpu 110 -> 2       skipped; a restart made a new cgroup
//	missed sweep     t30 -> t50         one 20 s pair
//
// The ring evicts
// by count, so after missed sweeps it can hold samples older than the window,
// and the time filter drops them.
func (usage *slidingWindowUsage) average(identifier string, now time.Time) (SlidingWindowUsage, error) {
	usage.mutex.Lock()
	defer usage.mutex.Unlock()

	ring, found := usage.rings[identifier]
	if !found {
		return SlidingWindowUsage{}, ErrNotEnoughSamples
	}

	start := now.Add(-usage.window)
	var cpuMicroseconds uint64
	var upTime time.Duration
	for index := 1; index < ring.count; index++ {
		previous, current := ring.at(index-1), ring.at(index)
		elapsed := current.timestamp.Sub(previous.timestamp)
		// A restart recreates the cgroup and resets its CPU counter.
		if previous.timestamp.Before(start) || !previous.isUp || !current.isUp || elapsed <= 0 || current.cpuMicroseconds < previous.cpuMicroseconds {
			continue
		}

		upTime += elapsed
		cpuMicroseconds += current.cpuMicroseconds - previous.cpuMicroseconds
	}

	if upTime == 0 {
		return SlidingWindowUsage{}, ErrNotEnoughSamples
	}
	return SlidingWindowUsage{MilliCPUs: cpuMicroseconds * 1000 / uint64(upTime.Microseconds())}, nil
}

// forget drops the VM's ring. A sweep that stored a sample just before the VM's
// records were removed can create it again; it holds at most one window of
// samples until metald restarts.
func (usage *slidingWindowUsage) forget(identifier string) {
	usage.mutex.Lock()
	defer usage.mutex.Unlock()

	delete(usage.rings, identifier)
}

// RecordMetrics keeps a record that the metrics sampler has stored.
func (manager *Manager) RecordMetrics(identifier string, record metrics.Record) {
	manager.slidingWindowUsage.record(identifier, newCPUSample(record))
}

// SlidingWindowUsage returns a VM's average use over the manager's sliding window.
// It averages only over intervals where the VM was up at both samples.
func (manager *Manager) SlidingWindowUsage(ctx context.Context, identifier string) (SlidingWindowUsage, error) {
	if manager.metrics == nil {
		return SlidingWindowUsage{}, errors.New("VM metrics store is not configured")
	}

	usage := manager.slidingWindowUsage
	if !usage.isSeeded(identifier) {
		now := time.Now()
		records, err := manager.metrics.History(ctx, identifier, now.Add(-usage.window), now)
		if err != nil {
			return SlidingWindowUsage{}, fmt.Errorf("read metrics history: %w", err)
		}

		stored := make([]cpuSample, 0, len(records))
		for _, record := range records {
			stored = append(stored, newCPUSample(record))
		}
		usage.seed(identifier, stored)
	}
	return usage.average(identifier, time.Now())
}
