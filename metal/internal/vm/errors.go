package vm

import (
	"errors"
	"fmt"
)

// ErrNotFound indicates that a virtual machine does not exist.
var ErrNotFound = errors.New("vm: not found")

// ErrConflict indicates that the current state blocks an operation.
var ErrConflict = errors.New("vm: conflict")

// ErrInUse indicates that a host artifact still has dependent virtual machines.
var ErrInUse = errors.New("vm: in use")

// ErrMetadataServiceTooLarge indicates that the guest values do not fit in MMDS.
var ErrMetadataServiceTooLarge = errors.New("vm: metadata service document is too large")

// TransitionError reports a missing state transition.
type TransitionError struct {
	DesiredState  State
	ObservedState State
}

// Error names the transition that the state machine does not define.
func (e *TransitionError) Error() string {
	return fmt.Sprintf("vm: no transition from %s to %s", e.ObservedState, e.DesiredState)
}

// Unwrap marks a missing transition as a state conflict.
func (e *TransitionError) Unwrap() error { return ErrConflict }
