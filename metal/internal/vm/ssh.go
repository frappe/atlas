package vm

import "io"

// SSHConn is an interactive SSH session to a guest.
type SSHConn interface {
	io.ReadWriteCloser
	Resize(cols, rows uint16) error
}
