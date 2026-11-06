package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/network/traffic"
)

// fakeTrafficMonitor records closure after worker completion.
type fakeTrafficMonitor struct {
	closeError     error
	closed         bool
	workerFinished *atomic.Bool
	sawWorkerDone  bool
}

func (monitor *fakeTrafficMonitor) Close() error {
	monitor.closed = true
	if monitor.workerFinished != nil {
		monitor.sawWorkerDone = monitor.workerFinished.Load()
	}
	return monitor.closeError
}

func discardLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

type fakeSnapshotUploadOwner struct {
	shutdown bool
}

func (owner *fakeSnapshotUploadOwner) Shutdown(context.Context) error {
	owner.shutdown = true
	return nil
}

type fakeSerialBroker struct {
	shutdown bool
}

func (owner *fakeSerialBroker) Shutdown() {
	owner.shutdown = true
}

type fakeSystemdConnection struct {
	closed bool
}

type fakeHTTPServer struct {
	startError error
	stopped    chan struct{}
}

func (server *fakeHTTPServer) Serve(net.Listener) error {
	if server.startError != nil {
		return server.startError
	}
	<-server.stopped
	return http.ErrServerClosed
}

func (server *fakeHTTPServer) Shutdown(context.Context) error {
	select {
	case <-server.stopped:
	default:
		close(server.stopped)
	}
	return nil
}

func (connection *fakeSystemdConnection) Close() {
	connection.closed = true
}

func TestDaemonShutdownOwnsBackgroundLifecycle(t *testing.T) {
	daemonContext, cancelDaemon := context.WithCancel(t.Context())
	snapshotUploads := &fakeSnapshotUploadOwner{}
	serialBroker := &fakeSerialBroker{}
	systemd := &fakeSystemdConnection{}
	lifecycle := newDaemon(
		daemonContext,
		cancelDaemon,
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		snapshotUploads,
		serialBroker,
		systemd,
	)
	workerStopped := make(chan struct{})
	lifecycle.StartWorker(func(workerContext context.Context) {
		<-workerContext.Done()
		close(workerStopped)
	})

	if err := lifecycle.Shutdown(t.Context()); err != nil {
		t.Fatalf("shutdown: %v", err)
	}

	select {
	case <-workerStopped:
	default:
		t.Fatal("background worker did not stop")
	}
	if !snapshotUploads.shutdown || !serialBroker.shutdown || !systemd.closed {
		t.Fatalf("owners were not stopped: uploads=%t consoles=%t systemd=%t", snapshotUploads.shutdown, serialBroker.shutdown, systemd.closed)
	}
}

func TestTrafficListenerDoesNotWaitForRestoration(t *testing.T) {
	daemonContext, cancelDaemon := context.WithCancel(t.Context())
	lifecycle := newDaemon(daemonContext, cancelDaemon, discardLogger(),
		&fakeSnapshotUploadOwner{}, &fakeSerialBroker{}, &fakeSystemdConnection{})
	events := make(chan traffic.Event, 2)
	started := make(chan string, 2)
	release := make(chan struct{})
	lifecycle.StartTrafficListener(events, func(ctx context.Context, event traffic.Event) error {
		started <- event.Target.VirtualMachineID
		select {
		case <-release:
		case <-ctx.Done():
		}
		return nil
	})

	events <- traffic.Event{Target: traffic.Target{VirtualMachineID: "vm-1"}}
	events <- traffic.Event{Target: traffic.Target{VirtualMachineID: "vm-2"}}
	for range 2 {
		select {
		case <-started:
		case <-time.After(time.Second):
			t.Fatal("traffic listener waited for a restoration")
		}
	}

	close(events)
	close(release)
	if err := lifecycle.Shutdown(t.Context()); err != nil {
		t.Fatal(err)
	}
}

func TestDaemonShutdownClosesTheTrafficMonitorAfterWorkers(t *testing.T) {
	daemonContext, cancelDaemon := context.WithCancel(t.Context())
	workerFinished := &atomic.Bool{}
	monitor := &fakeTrafficMonitor{workerFinished: workerFinished}
	lifecycle := newDaemon(daemonContext, cancelDaemon, discardLogger(),
		&fakeSnapshotUploadOwner{}, &fakeSerialBroker{}, &fakeSystemdConnection{})
	lifecycle.OwnTrafficMonitor(monitor)
	lifecycle.StartWorker(func(workerContext context.Context) {
		<-workerContext.Done()
		workerFinished.Store(true)
	})

	if err := lifecycle.Shutdown(t.Context()); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	if !monitor.closed {
		t.Fatal("the traffic monitor was not closed")
	}
	if !monitor.sawWorkerDone {
		t.Fatal("the traffic monitor closed before the worker finished")
	}
}

func TestDaemonShutdownReportsATrafficMonitorCloseError(t *testing.T) {
	daemonContext, cancelDaemon := context.WithCancel(t.Context())
	monitor := &fakeTrafficMonitor{closeError: errors.New("map still busy")}
	lifecycle := newDaemon(daemonContext, cancelDaemon, discardLogger(),
		&fakeSnapshotUploadOwner{}, &fakeSerialBroker{}, &fakeSystemdConnection{})
	lifecycle.OwnTrafficMonitor(monitor)

	err := lifecycle.Shutdown(t.Context())
	if err == nil || !strings.Contains(err.Error(), "traffic monitor") {
		t.Fatalf("error = %v, want the traffic monitor context", err)
	}
}

func TestDaemonShutdownWithoutATrafficMonitor(t *testing.T) {
	daemonContext, cancelDaemon := context.WithCancel(t.Context())
	lifecycle := newDaemon(daemonContext, cancelDaemon, discardLogger(),
		&fakeSnapshotUploadOwner{}, &fakeSerialBroker{}, &fakeSystemdConnection{})

	if err := lifecycle.Shutdown(t.Context()); err != nil {
		t.Fatalf("shutdown without a monitor returned %v", err)
	}
}

func TestDaemonShutdownWaitIsBounded(t *testing.T) {
	daemonContext, cancelDaemon := context.WithCancel(t.Context())
	lifecycle := newDaemon(
		daemonContext,
		cancelDaemon,
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		&fakeSnapshotUploadOwner{},
		&fakeSerialBroker{},
		&fakeSystemdConnection{},
	)
	releaseWorker := make(chan struct{})
	lifecycle.StartWorker(func(context.Context) { <-releaseWorker })

	shutdownContext, cancelShutdown := context.WithTimeout(t.Context(), time.Millisecond)
	defer cancelShutdown()
	if err := lifecycle.Shutdown(shutdownContext); err == nil {
		t.Fatal("shutdown did not report the worker timeout")
	}
	close(releaseWorker)
}

func TestDaemonShutdownDoesNotWaitForAServerExitThatServeConsumed(t *testing.T) {
	daemonContext, cancelDaemon := context.WithCancel(t.Context())
	lifecycle := newDaemon(daemonContext, cancelDaemon, discardLogger(),
		&fakeSnapshotUploadOwner{}, &fakeSerialBroker{}, &fakeSystemdConnection{})
	serveError := errors.New("listener failed")
	failed := &fakeHTTPServer{startError: serveError, stopped: make(chan struct{})}
	running := &fakeHTTPServer{stopped: make(chan struct{})}

	if err := lifecycle.Serve(listeningServer{server: failed}, listeningServer{server: running}); !errors.Is(err, serveError) {
		t.Fatalf("serve error = %v, want %v", err, serveError)
	}
	if err := lifecycle.Shutdown(t.Context()); err != nil {
		t.Fatalf("shutdown after one server exit = %v", err)
	}
}
