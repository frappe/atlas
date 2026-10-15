package reconciler

import (
	"sync"
	"time"
)

// maximumBackoff caps the wait between two attempts on one failing item.
const maximumBackoff = 5 * time.Minute

type failure struct {
	count   int
	retryAt time.Time
}

// backoff delays the retry of each item that keeps failing.
type backoff struct {
	interval time.Duration
	mutex    sync.Mutex
	failures map[string]failure
}

func newBackoff(interval time.Duration) *backoff {
	return &backoff{interval: interval, failures: map[string]failure{}}
}

// isWaiting reports whether id failed and its retry time has not come.
func (b *backoff) isWaiting(id string) bool {
	b.mutex.Lock()
	defer b.mutex.Unlock()

	failed, found := b.failures[id]
	return found && time.Now().Before(failed.retryAt)
}

// fail records a failure of id. The wait doubles from the interval up to maximumBackoff.
func (b *backoff) fail(id string) {
	b.mutex.Lock()
	defer b.mutex.Unlock()

	failed := b.failures[id]
	failed.count++
	wait := b.interval
	for range failed.count - 1 {
		wait = min(wait*2, maximumBackoff)
	}
	failed.retryAt = time.Now().Add(min(wait, maximumBackoff))
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
}
