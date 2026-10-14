package vm

import (
	"context"
	"sync"
	"time"
)

const (
	// Exclude requested changes from downtime during this grace period.
	availabilityGrace = 5 * time.Minute

	missedPingLimit = 3

	guestCheckConcurrency = 8
)

const (
	unavailableNotRunning  = "not_running"
	unavailableStuck       = "stuck"
	unavailableUnreachable = "unreachable"
)

var unavailableReasons = []string{unavailableNotRunning, unavailableStuck, unavailableUnreachable}

type availabilityTracker struct {
	mutex       sync.Mutex
	missedPings map[string]int
	unavailable map[string]unavailablePeriod
}

type unavailablePeriod struct {
	reason string
	since  time.Time
}

func newAvailabilityTracker() availabilityTracker {
	return availabilityTracker{missedPings: make(map[string]int), unavailable: make(map[string]unavailablePeriod)}
}

func availabilityOf(desired DesiredRecord, observed ObservedRecord, pendingFor time.Duration, missedPings int) (wanted bool, reason string) {
	if desired.State != StateRunning {
		return false, ""
	}

	applied := observed.Generation == desired.Generation && observed.RestartGeneration == desired.RestartGeneration
	switch {
	case !applied && pendingFor < availabilityGrace:
		return false, ""
	case !applied:
		return true, unavailableStuck
	case desired.Specification.SleepAfterIdleSeconds > 0 && observed.State == StateStopped:
		return false, ""
	case observed.State != StateRunning:
		return true, unavailableNotRunning
	case missedPings >= missedPingLimit:
		return true, unavailableUnreachable
	}
	return true, ""
}

// CheckGuests tracks consecutive missed pings for running guests.
func (manager *Manager) CheckGuests(ctx context.Context) error {
	identifiers, err := manager.store.listIDs()
	if err != nil {
		return err
	}

	results := make(map[string]bool)
	var resultsMutex sync.Mutex
	var workers sync.WaitGroup
	slots := make(chan struct{}, guestCheckConcurrency)
	for _, identifier := range identifiers {
		desired, observed, err := manager.newVirtualMachine(identifier).records()
		if err != nil || desired.State != StateRunning || observed.State != StateRunning {
			continue
		}
		slots <- struct{}{}
		workers.Go(func() {
			defer func() { <-slots }()
			answered := manager.runtime.Ping(ctx, runtimeMachine(desired, observed.NetworkInterface)) == nil
			resultsMutex.Lock()
			results[identifier] = answered
			resultsMutex.Unlock()
		})
	}
	workers.Wait()

	manager.availability.mutex.Lock()
	defer manager.availability.mutex.Unlock()
	for identifier := range manager.availability.missedPings {
		if _, checked := results[identifier]; !checked {
			delete(manager.availability.missedPings, identifier)
		}
	}
	for identifier, answered := range results {
		if answered {
			delete(manager.availability.missedPings, identifier)
		} else {
			manager.availability.missedPings[identifier]++
		}
	}
	return nil
}

func (manager *Manager) missedPings(identifier string) int {
	manager.availability.mutex.Lock()
	defer manager.availability.mutex.Unlock()
	return manager.availability.missedPings[identifier]
}

// Removed or unwanted VMs close any open downtime period.
func (manager *Manager) recordAvailability(reasons map[string]string) {
	manager.availability.mutex.Lock()
	defer manager.availability.mutex.Unlock()

	now := time.Now()
	for identifier, period := range manager.availability.unavailable {
		if reasons[identifier] == period.reason {
			continue
		}
		manager.logger.Info("virtual machine available again", "component", "vm", "virtual_machine_id", identifier,
			"reason", period.reason, "duration", now.Sub(period.since))
		delete(manager.availability.unavailable, identifier)
	}
	for identifier, reason := range reasons {
		if _, known := manager.availability.unavailable[identifier]; known || reason == "" {
			continue
		}
		manager.logger.Warn("virtual machine unavailable", "component", "vm", "virtual_machine_id", identifier, "reason", reason)
		manager.availability.unavailable[identifier] = unavailablePeriod{reason: reason, since: now}
	}
}
