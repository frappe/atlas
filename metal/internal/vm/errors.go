package vm

import (
	"errors"
	"fmt"
)

// ErrNotFound indicates that a virtual machine does not exist.
var ErrNotFound = errors.New("vm: not found")

// ErrConflict indicates that the current state blocks an operation.
var ErrConflict = errors.New("vm: conflict")

// TransitionError reports a missing state transition.
type TransitionError struct {
	DesiredState  State
	ObservedState State
}

func (e *TransitionError) Error() string {
	return fmt.Sprintf("vm: no transition from %s to %s", e.ObservedState, e.DesiredState)
}

// Unwrap marks a missing transition as a state conflict.
func (e *TransitionError) Unwrap() error { return ErrConflict }
