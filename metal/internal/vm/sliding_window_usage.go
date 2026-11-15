package vm

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/frappe/atlas/metal/internal/metrics"
)

// SlidingWindowUsage is a VM's average resource use over its sliding window.
type SlidingWindowUsage struct {
	// MilliCPUs is average CPU use in thousandths of a CPU. 1500 means 1.5 CPUs.
	MilliCPUs uint64
}

// slidingWindowUsage keeps the newest CPU samples of each burstable VM, so a read
// does not decode the daily metrics file. The metrics sampler feeds it after
// each stored record and refills a ring from the store when needed.
//
//	event                     sweep of vm1           ring of vm1               window
//	metald starts             -                      (none)                    -
//	first sweep stores t10    History, seed          [t-20 t-10 t0 t10]        1 min
//	sweep stores t20          record                 [t-10 t0 t10 t20]         1 min
//	window set to 2 min       History, seed          [t-80 ... t20 t30]        2 min
//	burst turned off          forget                 (none)                    -
//	destroy or migrate        forget                 (none)                    -
type slidingWindowUsage struct {
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
	// window is the VM's sliding window when the ring was built. The ring holds
	// window / metrics.SampleInterval + 1 samples, because N intervals need N + 1.
	window  time.Duration
	samples []cpuSample
	next    int
	count   int
}

func newSlidingWindowUsage() *slidingWindowUsage {
	return &slidingWindowUsage{rings: make(map[string]*cpuSampleRing)}
}

func newCPUSampleRing(window time.Duration) *cpuSampleRing {
	return &cpuSampleRing{window: window, samples: make([]cpuSample, window/metrics.SampleInterval+1)}
}

func newCPUSample(record metrics.Record) cpuSample {
	return cpuSample{
		timestamp:       record.Timestamp.Truncate(time.Second),
		isUp:            record.Metrics.Up,
		cpuMicroseconds: record.Metrics.CPUTimeMicroseconds,
	}
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

// record adds a stored sample to the VM's ring for window. It returns false when
// the VM has no such ring: after a metald restart, when the VM became burstable,
// or after its window changed.
func (usage *slidingWindowUsage) record(identifier string, window time.Duration, sample cpuSample) bool {
	usage.mutex.Lock()
	defer usage.mutex.Unlock()

	ring, found := usage.rings[identifier]
	if !found || ring.window != window {
		return false
	}
	ring.push(sample)
	return true
}

// seed replaces the VM's ring with a ring for window that holds the stored
// samples. A ring only gets samples that the store already has, and one sweep
// worker handles a VM at a time, so nothing in the old ring is lost.
func (usage *slidingWindowUsage) seed(identifier string, window time.Duration, stored []cpuSample) {
	usage.mutex.Lock()
	defer usage.mutex.Unlock()

	ring := newCPUSampleRing(window)
	for _, sample := range stored {
		ring.push(sample)
	}
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
//	missed sweep     t30 -> t50         one 20 s pair
//	counter falls    cpu 110 -> 2       a restart made a new cgroup that counts
//	                                    from 0, so the pair adds 2 over the full
//	                                    interval; the old cgroup's last seconds are lost
//
// A restart where the new counter is already above the old one (cpu 15 -> 18)
// adds 3 instead of 18; it cannot be seen without stored state. The ring evicts
// by count, so after missed sweeps it can hold samples older than the window,
// and the time filter drops them.
func (usage *slidingWindowUsage) average(identifier string, now time.Time) (SlidingWindowUsage, error) {
	usage.mutex.Lock()
	defer usage.mutex.Unlock()

	ring, found := usage.rings[identifier]
	if !found {
		return SlidingWindowUsage{}, ErrNotEnoughSamples
	}

	start := now.Add(-ring.window)
	var cpuMicroseconds uint64
	var upTime time.Duration
	for index := 1; index < ring.count; index++ {
		previous, current := ring.at(index-1), ring.at(index)
		elapsed := current.timestamp.Sub(previous.timestamp)
		if previous.timestamp.Before(start) || !previous.isUp || !current.isUp || elapsed <= 0 {
			continue
		}

		upTime += elapsed
		if current.cpuMicroseconds < previous.cpuMicroseconds {
			cpuMicroseconds += current.cpuMicroseconds
		} else {
			cpuMicroseconds += current.cpuMicroseconds - previous.cpuMicroseconds
		}
	}

	if upTime == 0 {
		return SlidingWindowUsage{}, ErrNotEnoughSamples
	}
	return SlidingWindowUsage{MilliCPUs: cpuMicroseconds * 1000 / uint64(upTime.Microseconds())}, nil
}

// forget drops the VM's ring when the VM leaves this host or stops being burstable.
func (usage *slidingWindowUsage) forget(identifier string) {
	usage.mutex.Lock()
	defer usage.mutex.Unlock()

	delete(usage.rings, identifier)
}

// RecordMetrics keeps the CPU samples of a burstable VM after the metrics
// sampler stored record. The first sweep after a metald restart, or after a
// window change, refills the VM's ring from the store.
func (manager *Manager) RecordMetrics(ctx context.Context, identifier string, record metrics.Record) error {
	if manager.metrics == nil {
		return fmt.Errorf("VM metrics store is not configured")
	}
	desired, err := manager.store.readDesired(identifier)
	if errors.Is(err, ErrNotFound) {
		manager.slidingWindowUsage.forget(identifier)
		return nil
	}
	if err != nil {
		return err
	}

	burst := desired.Specification.CPUBurst
	if !burst.IsBurstable {
		manager.slidingWindowUsage.forget(identifier)
		return nil
	}
	window := time.Duration(burst.SlidingWindowMinutes) * time.Minute
	if manager.slidingWindowUsage.record(identifier, window, newCPUSample(record)) {
		return nil
	}

	// The store already holds record, so History returns it too.
	now := time.Now()
	records, err := manager.metrics.History(ctx, identifier, now.Add(-window), now)
	if err != nil {
		return fmt.Errorf("read metrics history: %w", err)
	}
	stored := make([]cpuSample, 0, len(records))
	for _, storedRecord := range records {
		stored = append(stored, newCPUSample(storedRecord))
	}
	manager.slidingWindowUsage.seed(identifier, window, stored)
	return nil
}

// SlidingWindowUsage returns a burstable VM's average use over its sliding window.
// It averages only over intervals where the VM was up at both samples. After a
// window change, the old window applies until the next sweep.
func (manager *Manager) SlidingWindowUsage(identifier string) (SlidingWindowUsage, error) {
	desired, err := manager.store.readDesired(identifier)
	if err != nil {
		return SlidingWindowUsage{}, err
	}
	if !desired.Specification.CPUBurst.IsBurstable {
		return SlidingWindowUsage{}, ErrNotBurstable
	}
	return manager.slidingWindowUsage.average(identifier, time.Now())
}
