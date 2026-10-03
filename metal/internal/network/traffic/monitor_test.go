package traffic

import (
	"errors"
	"sync"
	"testing"
	"time"
)

type fakeHook struct {
	closed bool
}

type fakeTrafficHooks struct {
	packetTimes   map[uint32]uint64
	cleared       []uint32
	watching      map[uint32]bool
	rxCounters    map[uint32]TrafficCounters
	txCounters    map[uint32]TrafficCounters
	events        chan uint32
	readerClosed  chan struct{}
	closeOnce     sync.Once
	hooks         []*fakeHook
	resolvedIndex int
}

func newFakeTrafficHooks() *fakeTrafficHooks {
	return &fakeTrafficHooks{
		packetTimes:  make(map[uint32]uint64),
		watching:     make(map[uint32]bool),
		rxCounters:   make(map[uint32]TrafficCounters),
		txCounters:   make(map[uint32]TrafficCounters),
		events:       make(chan uint32, 1),
		readerClosed: make(chan struct{}),
	}
}

func (*fakeTrafficHooks) open(uint32) error { return nil }

func (hooks *fakeTrafficHooks) attach(uint32, string, string) (int, func() error, error) {
	hook := &fakeHook{}
	hooks.hooks = append(hooks.hooks, hook)
	return 7, func() error {
		hook.closed = true
		return nil
	}, nil
}

func (hooks *fakeTrafficHooks) interfaceIndex(string, string) (int, error) {
	if hooks.resolvedIndex == 0 {
		return 7, nil
	}
	return hooks.resolvedIndex, nil
}

func (hooks *fakeTrafficHooks) lastPacket(userID uint32) (uint64, bool, error) {
	value, found := hooks.packetTimes[userID]
	return value, found, nil
}

func (hooks *fakeTrafficHooks) setWatching(userID uint32, watching bool) error {
	hooks.watching[userID] = watching
	return nil
}

func (hooks *fakeTrafficHooks) clear(userID uint32) error {
	delete(hooks.packetTimes, userID)
	delete(hooks.watching, userID)
	delete(hooks.rxCounters, userID)
	delete(hooks.txCounters, userID)
	hooks.cleared = append(hooks.cleared, userID)
	return nil
}

func (hooks *fakeTrafficHooks) readTrafficCounters(userID uint32) (TrafficCounters, TrafficCounters, error) {
	return hooks.rxCounters[userID], hooks.txCounters[userID], nil
}

func (hooks *fakeTrafficHooks) readEvent() (uint32, error) {
	select {
	case userID := <-hooks.events:
		return userID, nil
	case <-hooks.readerClosed:
		return 0, errEventReaderClosed
	}
}

func (hooks *fakeTrafficHooks) closeEventReader() error {
	hooks.closeOnce.Do(func() { close(hooks.readerClosed) })
	return nil
}

func (*fakeTrafficHooks) closeMaps() error { return nil }

func newTestMonitor(t *testing.T) (*Monitor, *fakeTrafficHooks) {
	t.Helper()
	hooks := newFakeTrafficHooks()
	monitor, err := newMonitor(Config{MinimumUserID: 1000, MaximumUserID: 1010}, hooks)
	if err != nil {
		t.Fatal(err)
	}
	monitor.clock = func() (uint64, error) { return uint64(10 * time.Second), nil }
	t.Cleanup(func() { _ = monitor.Close() })
	return monitor, hooks
}

func TestMonitorRejectsAnInvalidUserIDRange(t *testing.T) {
	_, err := newMonitor(Config{MinimumUserID: 2, MaximumUserID: 1}, newFakeTrafficHooks())
	if err == nil {
		t.Fatal("want an invalid range error")
	}
}

func TestMonitorSamplesTrafficAndPublishesAnEvent(t *testing.T) {
	monitor, hooks := newTestMonitor(t)
	target := Target{VirtualMachineID: "vm-1", UserID: 1001}
	if err := monitor.Attach(AttachmentRequest{Target: target, NamespacePath: "/run/netns/vm-1", InterfaceName: "tap0"}); err != nil {
		t.Fatal(err)
	}
	hooks.packetTimes[target.UserID] = uint64(7 * time.Second)

	sample, err := monitor.Sample(target)
	if err != nil {
		t.Fatal(err)
	}
	if sample.IdleFor != 3*time.Second || sample.PacketSequence != uint64(7*time.Second) {
		t.Fatalf("sample = %+v", sample)
	}

	if err := monitor.StartWatching(target); err != nil {
		t.Fatal(err)
	}
	hooks.events <- target.UserID
	select {
	case event := <-monitor.Events():
		if event.Target != target {
			t.Fatalf("event target = %+v", event.Target)
		}
	case <-time.After(time.Second):
		t.Fatal("traffic event was not published")
	}
}

func TestMonitorCountersReadsBothDirections(t *testing.T) {
	monitor, hooks := newTestMonitor(t)
	target := Target{VirtualMachineID: "vm-1", UserID: 1001}
	if err := monitor.Attach(AttachmentRequest{Target: target, InterfaceName: "tap0"}); err != nil {
		t.Fatal(err)
	}
	hooks.rxCounters[target.UserID] = TrafficCounters{Bytes: 4096, Packets: 4}
	hooks.txCounters[target.UserID] = TrafficCounters{Bytes: 512, Packets: 2}

	received, sent, err := monitor.GetTrafficCounters(target)
	if err != nil {
		t.Fatal(err)
	}
	if received != (TrafficCounters{Bytes: 4096, Packets: 4}) {
		t.Fatalf("received = %+v", received)
	}
	if sent != (TrafficCounters{Bytes: 512, Packets: 2}) {
		t.Fatalf("sent = %+v", sent)
	}
}

func TestMonitorCountersRejectsAnUnknownTarget(t *testing.T) {
	monitor, _ := newTestMonitor(t)
	_, _, err := monitor.GetTrafficCounters(Target{VirtualMachineID: "vm-1", UserID: 1001})
	if !errors.Is(err, ErrNotFound) {
		t.Fatalf("err = %v, want %v", err, ErrNotFound)
	}
}

func TestMonitorDetachRemovesTheAttachment(t *testing.T) {
	monitor, hooks := newTestMonitor(t)
	target := Target{VirtualMachineID: "vm-1", UserID: 1001}
	if err := monitor.Attach(AttachmentRequest{Target: target, InterfaceName: "tap0"}); err != nil {
		t.Fatal(err)
	}
	if err := monitor.Detach(target.VirtualMachineID); err != nil {
		t.Fatal(err)
	}
	if !hooks.hooks[0].closed {
		t.Fatal("hook was not closed")
	}
	if _, err := monitor.Sample(target); !errors.Is(err, ErrNotFound) {
		t.Fatalf("error = %v, want ErrNotFound", err)
	}
}

func TestMonitorReplacesAChangedTap(t *testing.T) {
	monitor, hooks := newTestMonitor(t)
	request := AttachmentRequest{Target: Target{VirtualMachineID: "vm-1", UserID: 1001}, InterfaceName: "tap0"}
	if err := monitor.Attach(request); err != nil {
		t.Fatal(err)
	}
	hooks.cleared = nil
	hooks.resolvedIndex = 8
	if err := monitor.Attach(request); err != nil {
		t.Fatal(err)
	}
	if !hooks.hooks[0].closed || len(hooks.hooks) != 2 {
		t.Fatalf("first hook closed = %t, hooks = %d", hooks.hooks[0].closed, len(hooks.hooks))
	}
	if len(hooks.cleared) != 2 {
		t.Fatalf("cleared user IDs = %v", hooks.cleared)
	}
}

func TestMonitorCloseClosesTheEventChannel(t *testing.T) {
	monitor, _ := newTestMonitor(t)
	if err := monitor.Close(); err != nil {
		t.Fatal(err)
	}
	if _, open := <-monitor.Events(); open {
		t.Fatal("event channel is still open")
	}
}
