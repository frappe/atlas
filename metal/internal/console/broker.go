// Package console manages VM serial consoles over PTYs.
package console

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"

	"github.com/creack/pty"
)

// defaultScrollbackBytes is the console history a new viewer receives.
const defaultScrollbackBytes = 128 << 10

// subscriberBufferBytes is the per-viewer output buffer.
const subscriberBufferBytes = 256 << 10

// ErrConsoleNotFound reports that a VM has no open console.
var ErrConsoleNotFound = errors.New("console: not found")

// ErrConsoleBusy reports that a console has too many viewers.
var ErrConsoleBusy = errors.New("console: too many viewers")

// maxSubscribers caps viewers on one console.
const maxSubscribers = 16

// Winsize is a viewer terminal size applied to the PTY.
type Winsize struct {
	Rows uint16
	Cols uint16
}

// Broker owns the serial consoles of all running virtual machines.
type Broker struct {
	directory       string
	scrollbackBytes int

	mutex    sync.Mutex
	consoles map[string]*console
}

// NewBroker returns a Broker that keeps the PTY slave links under directory.
func NewBroker(directory string) *Broker {
	return &Broker{
		directory:       directory,
		scrollbackBytes: defaultScrollbackBytes,
		consoles:        make(map[string]*console),
	}
}

// Open allocates a PTY for a VM and links its slave to a stable path.
// Open is idempotent.
func (b *Broker) Open(id string) error {
	b.mutex.Lock()
	defer b.mutex.Unlock()

	if _, found := b.consoles[id]; found {
		return nil
	}

	master, slave, err := pty.Open()
	if err != nil {
		return fmt.Errorf("open console pty: %w", err)
	}

	if err := os.MkdirAll(b.directory, 0o750); err != nil {
		master.Close()
		slave.Close()
		return fmt.Errorf("create console directory: %w", err)
	}
	link := filepath.Join(b.directory, id)
	if err := os.Remove(link); err != nil && !os.IsNotExist(err) {
		master.Close()
		slave.Close()
		return fmt.Errorf("clear console link: %w", err)
	}
	if err := os.Symlink(slave.Name(), link); err != nil {
		master.Close()
		slave.Close()
		return fmt.Errorf("link console slave: %w", err)
	}
	// Keep only the master. systemd reopens the slave through the link.
	slave.Close()

	b.consoles[id] = newConsole(master, link, b.scrollbackBytes)
	return nil
}

// Close stops and removes a VM's console. Close is idempotent.
func (b *Broker) Close(id string) error {
	b.mutex.Lock()
	target := b.consoles[id]
	delete(b.consoles, id)
	b.mutex.Unlock()

	if target == nil {
		return nil
	}
	return target.close()
}

// Attach streams a VM's console to one viewer until it disconnects.
func (b *Broker) Attach(ctx context.Context, id string, client io.ReadWriter, resize <-chan Winsize) error {
	b.mutex.Lock()
	target := b.consoles[id]
	b.mutex.Unlock()

	if target == nil {
		return ErrConsoleNotFound
	}
	return target.attach(ctx, client, resize)
}

// Shutdown closes every open console.
func (b *Broker) Shutdown() {
	b.mutex.Lock()
	consoles := b.consoles
	b.consoles = make(map[string]*console)
	b.mutex.Unlock()

	for _, target := range consoles {
		_ = target.close()
	}
}
