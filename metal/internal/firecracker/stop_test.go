package firecracker

import (
	"context"
	"net"
	"net/http"
	"path/filepath"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/systemd"
	"github.com/frappe/atlas/metal/internal/vm"
)

// stubUnits is a systemd.Manager whose unit stays active until it is stopped or
// killed. Wait blocks while the unit is active, like the D-Bus manager does.
// A killed unit reports "failed" until ResetFailed clears it, as systemd does.
type stubUnits struct {
	mu     sync.Mutex
	active bool
	failed bool
	stops  int
	kills  int
	waits  int
}

func (s *stubUnits) shutdown() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.active = false
}

func (s *stubUnits) Start(context.Context, string) error { return nil }

func (s *stubUnits) Stop(context.Context, string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.stops++
	s.active = false
	return nil
}

func (s *stubUnits) Kill(context.Context, string, syscall.Signal) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.kills++
	s.active = false
	s.failed = true
	return nil
}

func (s *stubUnits) ResetFailed(context.Context, string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.failed = false
	return nil
}

func (s *stubUnits) Status(context.Context, string) (systemd.Status, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	switch {
	case s.failed:
		return systemd.Status{ActiveState: "failed"}, nil
	case s.active:
		return systemd.Status{ActiveState: "active"}, nil
	}
	return systemd.Status{ActiveState: "inactive"}, nil
}

func (s *stubUnits) Wait(ctx context.Context, _ string) (systemd.Result, error) {
	s.mu.Lock()
	s.waits++
	s.mu.Unlock()
	for {
		s.mu.Lock()
		up := s.active
		s.mu.Unlock()
		if !up {
			return systemd.Result{}, nil
		}
		select {
		case <-ctx.Done():
			return systemd.Result{}, ctx.Err()
		case <-time.After(time.Millisecond):
		}
	}
}

func (s *stubUnits) List(context.Context) ([]string, error)                  { return nil, nil }
func (s *stubUnits) SetLimits(context.Context, string, systemd.Limits) error { return nil }

func (s *stubUnits) counts() (stops, kills, waits int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.stops, s.kills, s.waits
}

// fcSocket serves a firecracker API socket that accepts every request and runs
// onRequest, which stands in for the guest reacting to the action.
func fcSocket(t *testing.T, onRequest func()) string {
	t.Helper()
	sock := filepath.Join(t.TempDir(), "fc.socket")
	l, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if onRequest != nil {
			onRequest()
		}
		w.WriteHeader(http.StatusNoContent)
	})}
	go srv.Serve(l)
	t.Cleanup(func() { srv.Close() })
	return sock
}

func testMachine(units systemd.Manager, sock string, timeout time.Duration) *machine {
	return &machine{
		d:           &Driver{units: units},
		cfg:         vmConfig{ID: "abc", Sock: sock},
		api:         api.New(sock),
		stopTimeout: timeout,
	}
}

// A guest without an i8042 keyboard driver never sees Ctrl+Alt+Del, so Stop must
// give up waiting and let systemd terminate the VM.
func TestStopEscalatesWhenGuestIgnoresCtrlAltDel(t *testing.T) {
	units := &stubUnits{active: true}
	m := testMachine(units, fcSocket(t, nil), 20*time.Millisecond)

	if err := m.Stop(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	stops, kills, _ := units.counts()
	if stops != 1 {
		t.Errorf("systemd stops = %d, want 1", stops)
	}
	if kills != 0 {
		t.Errorf("kills = %d, want 0", kills)
	}
}

func TestStopLetsGuestShutItselfDown(t *testing.T) {
	units := &stubUnits{active: true}
	m := testMachine(units, fcSocket(t, units.shutdown), time.Minute)

	if err := m.Stop(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	stops, kills, waits := units.counts()
	if stops != 0 || kills != 0 {
		t.Errorf("stops = %d, kills = %d, want 0 and 0", stops, kills)
	}
	if waits == 0 {
		t.Error("Stop did not wait for the guest to exit")
	}
}

func TestStopForceKills(t *testing.T) {
	units := &stubUnits{active: true}
	m := testMachine(units, fcSocket(t, nil), time.Minute)

	if err := m.Stop(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	stops, kills, waits := units.counts()
	if kills != 1 {
		t.Errorf("kills = %d, want 1", kills)
	}
	if stops != 0 {
		t.Errorf("systemd stops = %d, want 0", stops)
	}
	if waits == 0 {
		t.Error("Stop did not wait for the process to exit")
	}
}

// systemd flags a unit killed out of band as failed. A VM stopped on purpose
// must still report StateStopped.
func TestStopClearsTheFailedUnitState(t *testing.T) {
	for _, force := range []bool{true, false} {
		units := &stubUnits{active: true}
		m := testMachine(units, fcSocket(t, nil), 20*time.Millisecond)

		if err := m.Stop(context.Background(), force); err != nil {
			t.Fatalf("force=%v: %v", force, err)
		}
		st, err := units.Status(context.Background(), m.cfg.ID)
		if err != nil {
			t.Fatal(err)
		}
		if got := m.state(context.Background(), st); got != vm.StateStopped {
			t.Errorf("force=%v: state = %q, want %q", force, got, vm.StateStopped)
		}
	}
}

// A VM that died on its own was not stopped on purpose, so it keeps StateFailed.
func TestCrashedVMReportsFailed(t *testing.T) {
	units := &stubUnits{failed: true}
	m := testMachine(units, fcSocket(t, nil), time.Minute)

	st, err := units.Status(context.Background(), m.cfg.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got := m.state(context.Background(), st); got != vm.StateFailed {
		t.Errorf("state = %q, want %q", got, vm.StateFailed)
	}
}
