package console

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/creack/pty"
)

// fakeClient records output and blocks on Read during tests.
type fakeClient struct {
	mutex   sync.Mutex
	buffer  bytes.Buffer
	release chan struct{}
}

func newFakeClient() *fakeClient {
	return &fakeClient{release: make(chan struct{})}
}

func (client *fakeClient) Write(data []byte) (int, error) {
	client.mutex.Lock()
	defer client.mutex.Unlock()
	return client.buffer.Write(data)
}

func (client *fakeClient) Read([]byte) (int, error) {
	<-client.release
	return 0, nil
}

func (client *fakeClient) written() []byte {
	client.mutex.Lock()
	defer client.mutex.Unlock()
	return append([]byte(nil), client.buffer.Bytes()...)
}

func TestRingBufferKeepsRecentBytes(t *testing.T) {
	ring := newRingBuffer(4)
	ring.write([]byte("ab"))
	ring.write([]byte("cde"))
	if got := string(ring.snapshot()); got != "bcde" {
		t.Fatalf("snapshot = %q, want %q", got, "bcde")
	}

	ring.write([]byte("0123456789"))
	if got := string(ring.snapshot()); got != "6789" {
		t.Fatalf("snapshot after large write = %q, want %q", got, "6789")
	}
}

// newTestConsole returns a console and its test PTY slave.
func newTestConsole(t *testing.T) (*console, *os.File) {
	t.Helper()
	master, slave, err := pty.Open()
	if err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(t.TempDir(), "console")
	c := newConsole(master, link, 1<<16)
	// Close the slave first so the drain read returns.
	t.Cleanup(func() { slave.Close(); _ = c.close() })
	return c, slave
}

func TestAttachReplaysScrollbackAndStreamsLive(t *testing.T) {
	c, slave := newTestConsole(t)

	if _, err := slave.Write([]byte("history\r\n")); err != nil {
		t.Fatal(err)
	}
	// Read the ring under its lock while the drain writes it.
	waitFor(t, func() bool {
		c.mutex.Lock()
		defer c.mutex.Unlock()
		return len(c.ring.snapshot()) > 0
	})

	client := newFakeClient()
	go func() { _ = c.attach(context.Background(), client, make(chan Winsize)) }()

	waitFor(t, func() bool { return bytes.Contains(client.written(), []byte("history")) })

	if _, err := slave.Write([]byte("live\r\n")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { return bytes.Contains(client.written(), []byte("live")) })
}

// blockingClient accepts no output until release closes.
type blockingClient struct {
	release chan struct{}
}

func (client *blockingClient) Write(data []byte) (int, error) {
	<-client.release
	return len(data), nil
}

// delayedClient records output after a network-like delay on each write.
type delayedClient struct {
	*fakeClient
	delay time.Duration
}

func (client delayedClient) Write(data []byte) (int, error) {
	time.Sleep(client.delay)
	return client.fakeClient.Write(data)
}

func viewerCount(c *console) int {
	c.mutex.Lock()
	defer c.mutex.Unlock()
	return len(c.viewers)
}

func TestAttachDropsASlowViewer(t *testing.T) {
	c, slave := newTestConsole(t)

	client := &blockingClient{release: make(chan struct{})}
	result := make(chan error, 1)
	go func() { result <- c.attach(context.Background(), client, make(chan Winsize)) }()
	waitFor(t, func() bool { return viewerCount(c) == 1 })

	go func() { _, _ = slave.Write(bytes.Repeat([]byte("x"), 4*viewerBufferBytes)) }()
	waitFor(t, func() bool { return viewerCount(c) == 0 })

	close(client.release)
	if err := <-result; !errors.Is(err, ErrViewerTooSlow) {
		t.Fatalf("attach = %v, want ErrViewerTooSlow", err)
	}
}

// Firecracker writes serial output one byte at a time. A viewer with network
// latency must keep up with such a burst.
func TestAttachKeepsAViewerThroughManySmallWrites(t *testing.T) {
	c, slave := newTestConsole(t)

	client := delayedClient{fakeClient: newFakeClient(), delay: 20 * time.Millisecond}
	go func() { _ = c.attach(context.Background(), client, make(chan Winsize)) }()
	waitFor(t, func() bool { return viewerCount(c) == 1 })

	output := bytes.Repeat([]byte("x"), 1000)
	for index := range output {
		if _, err := slave.Write(output[index : index+1]); err != nil {
			t.Fatal(err)
		}
	}

	waitFor(t, func() bool { return bytes.Equal(client.written(), output) })
	if viewerCount(c) != 1 {
		t.Fatal("viewer was dropped during a burst of small writes")
	}
}

func TestCloseIsIdempotent(t *testing.T) {
	c, _ := newTestConsole(t)
	if err := c.close(); err != nil {
		t.Fatal(err)
	}
	if err := c.close(); err != nil {
		t.Fatalf("second close: %v", err)
	}
}

func waitFor(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("condition not met before timeout")
}
