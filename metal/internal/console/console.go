package console

import (
	"context"
	"errors"
	"io"
	"os"
	"sync"
	"syscall"
	"time"

	"github.com/creack/pty"
)

// console owns one VM's PTY master.
type console struct {
	master *os.File
	link   string

	writeMutex sync.Mutex

	mutex       sync.Mutex
	closed      bool
	ring        *ringBuffer
	subscribers map[*subscriber]struct{}

	drainDone chan struct{}
}

// subscriber receives output for one viewer. Slow viewers are dropped.
type subscriber struct {
	output  chan []byte
	dropped chan struct{}
}

func newConsole(master *os.File, link string, scrollbackBytes int) *console {
	c := &console{
		master:      master,
		link:        link,
		ring:        newRingBuffer(scrollbackBytes),
		subscribers: make(map[*subscriber]struct{}),
		drainDone:   make(chan struct{}),
	}
	go c.drain()
	return c
}

// drain records history and broadcasts output without blocking Firecracker.
// Linux returns EIO when no PTY slave is open, so drain retries on EIO.
func (c *console) drain() {
	defer close(c.drainDone)

	buffer := make([]byte, 32<<10)
	for {
		count, err := c.master.Read(buffer)
		if count > 0 {
			c.broadcast(buffer[:count])
		}
		if err == nil {
			continue
		}
		if errors.Is(err, syscall.EIO) {
			time.Sleep(20 * time.Millisecond)
			continue
		}
		return
	}
}

func (c *console) broadcast(data []byte) {
	chunk := make([]byte, len(data))
	copy(chunk, data)

	c.mutex.Lock()
	defer c.mutex.Unlock()

	c.ring.write(chunk)
	for target := range c.subscribers {
		select {
		case target.output <- chunk:
		default:
			// Drop slow viewers so the drain keeps running.
			delete(c.subscribers, target)
			close(target.dropped)
		}
	}
}

func (c *console) attach(ctx context.Context, client io.ReadWriter, resize <-chan Winsize) error {
	target := &subscriber{
		output:  make(chan []byte, subscriberBufferBytes/(32<<10)+1),
		dropped: make(chan struct{}),
	}

	c.mutex.Lock()
	if c.closed {
		c.mutex.Unlock()
		return ErrConsoleNotFound
	}
	if len(c.subscribers) >= maxSubscribers {
		c.mutex.Unlock()
		return ErrConsoleBusy
	}
	history := c.ring.snapshot()
	c.subscribers[target] = struct{}{}
	c.mutex.Unlock()

	defer c.removeSubscriber(target)

	if len(history) > 0 {
		if _, err := client.Write(history); err != nil {
			return err
		}
	}

	attachContext, cancel := context.WithCancel(ctx)
	defer cancel()

	go c.copyInput(attachContext, client, cancel)

	for {
		select {
		case <-attachContext.Done():
			return nil
		case <-target.dropped:
			return nil
		case size := <-resize:
			_ = pty.Setsize(c.master, &pty.Winsize{Rows: size.Rows, Cols: size.Cols})
		case chunk := <-target.output:
			if _, err := client.Write(chunk); err != nil {
				return err
			}
		}
	}
}

// copyInput forwards viewer input to the PTY master.
func (c *console) copyInput(ctx context.Context, client io.Reader, cancel context.CancelFunc) {
	defer cancel()

	buffer := make([]byte, 4<<10)
	for {
		count, err := client.Read(buffer)
		if count > 0 {
			c.writeMaster(buffer[:count])
		}
		if err != nil {
			return
		}
		if ctx.Err() != nil {
			return
		}
	}
}

func (c *console) writeMaster(data []byte) {
	c.writeMutex.Lock()
	defer c.writeMutex.Unlock()
	_, _ = c.master.Write(data)
}

func (c *console) removeSubscriber(target *subscriber) {
	c.mutex.Lock()
	delete(c.subscribers, target)
	c.mutex.Unlock()
}

func (c *console) close() error {
	c.mutex.Lock()
	if c.closed {
		c.mutex.Unlock()
		return nil
	}
	c.closed = true
	c.mutex.Unlock()

	// Closing the master ends the drain and attached readers.
	err := c.master.Close()
	<-c.drainDone
	if removeErr := os.Remove(c.link); removeErr != nil && !os.IsNotExist(removeErr) && err == nil {
		err = removeErr
	}
	return err
}
