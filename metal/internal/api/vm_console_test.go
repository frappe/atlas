package api

import (
	"context"
	"io"
	"net"
	"net/http"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/frappe/atlas/metal/internal/console"
)

// fakeSSHSession stays attached until its viewer input ends, as the console
// broker does, and counts Close calls.
type fakeSSHSession struct {
	closes atomic.Int32
}

func (session *fakeSSHSession) ID() string { return "ssh-0123456789abcdef" }

func (session *fakeSSHSession) Attach(_ context.Context, client io.ReadWriter, _ <-chan console.Winsize) error {
	_, _ = io.Copy(io.Discard, client)
	return nil
}

func (session *fakeSSHSession) Close() error {
	session.closes.Add(1)
	return nil
}

// startConsoleServer serves the API on a local port and returns the server and console URL.
func startConsoleServer(t *testing.T, path string, session *fakeSSHSession) (*http.Server, string) {
	t.Helper()

	manager := &fakeVirtualMachineManager{virtualMachines: map[string]*fakeVM{}, sshSession: session}
	server := newHTTPServer(t, manager, newFakeRuntimeServices(), &fakeWireGuardManager{})
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { _ = server.Close() })

	return server, "ws://" + listener.Addr().String() + path
}

// The Atlas bridge reconnects only after close code 1012, so a shutdown must send it.
func TestShutdownClosesAnOpenConsoleWithServiceRestart(t *testing.T) {
	router, url := startConsoleServer(t, "/v1/vms/"+openConsoleID+"/console", nil)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	connection, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		t.Fatalf("open console: %v", err)
	}
	defer connection.CloseNow()

	if err := router.Shutdown(ctx); err != nil {
		t.Fatalf("shut down: %v", err)
	}

	_, _, err = connection.Read(ctx)
	if status := websocket.CloseStatus(err); status != websocket.StatusServiceRestart {
		t.Fatalf("console closed with %v (%v), want %v", status, err, websocket.StatusServiceRestart)
	}
}

// A metald shutdown keeps the SSH session, so the viewer can resume the same shell.
func TestShutdownKeepsTheSSHSession(t *testing.T) {
	session := &fakeSSHSession{}
	router, url := startConsoleServer(t, "/v1/vms/vm-1/console?mode=ssh", session)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	connection, response, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		t.Fatalf("open console: %v", err)
	}
	defer connection.CloseNow()
	if got := response.Header.Get(sshSessionHeader); got != session.ID() {
		t.Fatalf("session header is %q, want %q", got, session.ID())
	}

	if err := router.Shutdown(ctx); err != nil {
		t.Fatalf("shut down: %v", err)
	}
	_, _, err = connection.Read(ctx)
	if status := websocket.CloseStatus(err); status != websocket.StatusServiceRestart {
		t.Fatalf("console closed with %v (%v), want %v", status, err, websocket.StatusServiceRestart)
	}
	if closes := session.closes.Load(); closes != 0 {
		t.Fatalf("shutdown closed the session %d times", closes)
	}
}

// A viewer that leaves ends its SSH session, which removes the guest key.
func TestALeavingViewerEndsTheSSHSession(t *testing.T) {
	session := &fakeSSHSession{}
	_, url := startConsoleServer(t, "/v1/vms/vm-1/console?mode=ssh", session)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	connection, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		t.Fatalf("open console: %v", err)
	}
	connection.Close(websocket.StatusNormalClosure, "")

	for session.closes.Load() == 0 {
		if ctx.Err() != nil {
			t.Fatal("the session did not close after the viewer left")
		}
		time.Sleep(10 * time.Millisecond)
	}
}
