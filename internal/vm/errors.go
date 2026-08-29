package vm

import "errors"

// ErrNotFound is returned when a driver has no record of a VM id.
var ErrNotFound = errors.New("vm: not found")
