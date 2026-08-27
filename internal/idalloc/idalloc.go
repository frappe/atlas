// Package idalloc hands out per-VM ids from a reserved range. It holds no state:
// callers pass the currently-used ids (reconstructed by scanning live VMs), so
// metald stays stateless.
package idalloc

import "errors"

// Range is an inclusive range of ids. metal uses one id per VM for both uid and
// gid (a private per-VM group).
type Range struct {
	Min, Max uint32
}

// DefaultRange is the reserved id range (the classic subuid range).
var DefaultRange = Range{Min: 100000, Max: 165535}

var ErrExhausted = errors.New("idalloc: range exhausted")

// Allocate returns the lowest id in r not present in used.
func (r Range) Allocate(used map[uint32]bool) (uint32, error) {
	for id := uint64(r.Min); id <= uint64(r.Max); id++ {
		if !used[uint32(id)] {
			return uint32(id), nil
		}
	}
	return 0, ErrExhausted
}
