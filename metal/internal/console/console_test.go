package console

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/creack/pty"
)

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
