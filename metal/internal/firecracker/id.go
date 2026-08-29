package firecracker

import (
	"crypto/rand"
	"encoding/hex"
)

// newID returns a short random hex id, safe as a systemd instance name, netns
// name and LVM tag.
func newID() string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}
