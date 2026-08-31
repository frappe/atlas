package vm

import "errors"

// ErrNotFound is returned when a driver has no record of a VM id.
var ErrNotFound = errors.New("vm: not found")

// ErrConflict is returned when an operation is invalid for the VM's current
// state, e.g. restoring a disk snapshot while the VM is running.
var ErrConflict = errors.New("vm: conflict")
