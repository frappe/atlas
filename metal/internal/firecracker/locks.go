package firecracker

import (
	"context"
	"sync"
)

type operationLock struct {
	available  chan struct{}
	references int
}

type operationLocks struct {
	mutex sync.Mutex
	locks map[string]*operationLock
}

func (locks *operationLocks) lock(ctx context.Context, id string) (func(), error) {
	entry := locks.reference(id)

	select {
	case <-entry.available:
		return func() {
			entry.available <- struct{}{}
			locks.release(id, entry)
		}, nil
	case <-ctx.Done():
		locks.release(id, entry)
		return nil, ctx.Err()
	}
}

func (locks *operationLocks) reference(id string) *operationLock {
	locks.mutex.Lock()
	defer locks.mutex.Unlock()

	if locks.locks == nil {
		locks.locks = make(map[string]*operationLock)
	}
	entry := locks.locks[id]
	if entry == nil {
		entry = &operationLock{available: make(chan struct{}, 1)}
		entry.available <- struct{}{}
		locks.locks[id] = entry
	}
	entry.references++
	return entry
}

func (locks *operationLocks) release(id string, entry *operationLock) {
	locks.mutex.Lock()
	defer locks.mutex.Unlock()

	entry.references--
	if entry.references == 0 {
		delete(locks.locks, id)
	}
}
