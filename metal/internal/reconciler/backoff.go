package reconciler

import (
	"sync"
	"time"
)

// maximumBackoff caps the wait between two attempts on one failing item.
const maximumBackoff = time.Minute

type failure struct {
	count   int
	retryAt time.Time
}

// backoff delays the retry of each item that keeps failing.
type backoff struct {
	interval time.Duration
	now      func() time.Time
	mutex    sync.Mutex
	failures map[string]failure
	resetAt  time.Time
}

func newBackoff(interval time.Duration) *backoff {
	return &backoff{interval: interval, now: time.Now, failures: map[string]failure{}}
}

// isWaiting reports whether id failed and its retry time has not come.
func (b *backoff) isWaiting(id string) bool {
	b.mutex.Lock()
	defer b.mutex.Unlock()

	failed, found := b.failures[id]
	return found && b.now().Before(failed.retryAt)
}

// fail records a failure of an attempt that began at startedAt. The wait doubles from the
// interval up to maximumBackoff. An attempt that began before the last reset is ignored.
func (b *backoff) fail(id string, startedAt time.Time) {
	b.mutex.Lock()
	defer b.mutex.Unlock()

	if startedAt.Before(b.resetAt) {
		return
	}

	failed := b.failures[id]
	failed.count++
	wait := b.interval
	for range failed.count - 1 {
		wait = min(wait*2, maximumBackoff)
	}
	failed.retryAt = b.now().Add(min(wait, maximumBackoff))
	b.failures[id] = failed
}

func (b *backoff) succeed(id string) {
	b.mutex.Lock()
	defer b.mutex.Unlock()

	delete(b.failures, id)
}

func (b *backoff) reset() {
	b.mutex.Lock()
	defer b.mutex.Unlock()

	clear(b.failures)
	b.resetAt = b.now()
}
