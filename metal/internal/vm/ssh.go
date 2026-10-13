package vm

import (
	"context"
	"io"

	"github.com/frappe/atlas/metal/internal/console"
)

// SSHSession is a guest shell that outlives a metald restart.
// A viewer attaches to it again by its ID.
type SSHSession interface {
	ID() string
	Attach(ctx context.Context, client io.ReadWriter, resize <-chan console.Winsize) error
	Close() error
}
