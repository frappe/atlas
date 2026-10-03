// Package traffic monitors IP traffic between the host and each guest.
package traffic

import (
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"golang.org/x/sys/unix"
)

const (
	maximumMapEntries = 1 << 20
	eventChannelSize  = 128
	readRetryDelay    = 100 * time.Millisecond
)

// ErrNotFound means the monitor has no matching attachment.
var ErrNotFound = errors.New("traffic attachment not found")

var errEventReaderClosed = errors.New("traffic event reader is closed")

// Target identifies one virtual machine attachment.
type Target struct {
	VirtualMachineID string
	UserID           uint32
}

// AttachmentRequest identifies one TAP device to monitor.
type AttachmentRequest struct {
	Target        Target
	NamespacePath string
	InterfaceName string
}

// Sample describes traffic since the attachment was created.
type Sample struct {
	IdleFor        time.Duration
	PacketSequence uint64
}

// Event reports traffic for a watched target.
type Event struct {
	Target Target
}

// Config contains monitor settings.
type Config struct {
	MinimumUserID uint32
	MaximumUserID uint32
	Logger        *slog.Logger
}

// Monitor owns traffic attachments and packet events.
type Monitor struct {
	capacity uint32
	hooks    trafficHooks
	logger   *slog.Logger
	clock    func() (uint64, error)

	mutex       sync.Mutex
	loaded      bool
	closed      bool
	attachments map[string]*attachment
	byUserID    map[uint32]string
	workerDone  chan struct{}

	events     chan Event
	closing    chan struct{}
	closeOnce  sync.Once
	closeError error
}

type attachment struct {
	target         Target
	namespacePath  string
	interfaceName  string
	interfaceIndex int
	baseline       uint64
	closeHook      func() error
}

type trafficHooks interface {
	open(capacity uint32) error
	attach(userID uint32, namespacePath, interfaceName string) (interfaceIndex int, closeHook func() error, err error)
	interfaceIndex(namespacePath, interfaceName string) (int, error)
	lastPacket(userID uint32) (nanoseconds uint64, found bool, err error)
	setWatching(userID uint32, watching bool) error
	clear(userID uint32) error
	readTrafficCounters(userID uint32) (rx, tx TrafficCounters, err error)
	readEvent() (userID uint32, err error)
	closeEventReader() error
	closeMaps() error
}

// NewMonitor returns a monitor that loads eBPF resources on its first attachment.
func NewMonitor(configuration Config) (*Monitor, error) {
	return newMonitor(configuration, &bpfHooks{})
}

// newMonitor creates a monitor with the supplied kernel hook implementation.
func newMonitor(configuration Config, hooks trafficHooks) (*Monitor, error) {
	capacity, err := configuration.capacity()
	if err != nil {
		return nil, err
	}
	if configuration.Logger == nil {
		configuration.Logger = slog.Default()
	}

	return &Monitor{
		capacity:    capacity,
		hooks:       hooks,
		logger:      configuration.Logger,
		clock:       readMonotonicNanoseconds,
		attachments: make(map[string]*attachment),
		byUserID:    make(map[uint32]string),
		events:      make(chan Event, eventChannelSize),
		closing:     make(chan struct{}),
	}, nil
}

// capacity returns the number of user IDs covered by the monitor maps.
func (configuration Config) capacity() (uint32, error) {
	if configuration.MaximumUserID < configuration.MinimumUserID {
		return 0, errors.New("traffic user ID range is invalid")
	}
	capacity := configuration.MaximumUserID - configuration.MinimumUserID + 1
	if capacity == 0 || capacity > maximumMapEntries {
		return 0, fmt.Errorf("traffic map capacity %d is invalid", capacity)
	}
	return capacity, nil
}

// Attach monitors traffic on one TAP device.
func (monitor *Monitor) Attach(request AttachmentRequest) error {
	monitor.mutex.Lock()
	defer monitor.mutex.Unlock()

	if monitor.closed {
		return errors.New("traffic monitor is closed")
	}
	if err := monitor.load(); err != nil {
		return err
	}

	existing := monitor.attachments[request.Target.VirtualMachineID]
	if existing != nil && monitor.attachmentMatches(existing, request) {
		return nil
	}

	interfaceIndex, closeHook, err := monitor.hooks.attach(request.Target.UserID, request.NamespacePath, request.InterfaceName)
	if err != nil {
		return fmt.Errorf("attach traffic monitor for VM %s: %w", request.Target.VirtualMachineID, err)
	}
	baseline, err := monitor.clock()
	if err != nil {
		return errors.Join(fmt.Errorf("read traffic clock for VM %s: %w", request.Target.VirtualMachineID, err), closeHook())
	}

	monitor.attachments[request.Target.VirtualMachineID] = &attachment{
		target:         request.Target,
		namespacePath:  request.NamespacePath,
		interfaceName:  request.InterfaceName,
		interfaceIndex: interfaceIndex,
		baseline:       baseline,
		closeHook:      closeHook,
	}
	monitor.byUserID[request.Target.UserID] = request.Target.VirtualMachineID

	if existing == nil {
		return monitor.hooks.clear(request.Target.UserID)
	}
	delete(monitor.byUserID, existing.target.UserID)
	monitor.byUserID[request.Target.UserID] = request.Target.VirtualMachineID
	return errors.Join(existing.closeHook(), monitor.hooks.clear(existing.target.UserID), monitor.hooks.clear(request.Target.UserID))
}

// attachmentMatches reports whether an existing attachment still targets the same TAP.
// Detach stops monitoring one virtual machine.
func (monitor *Monitor) Detach(virtualMachineID string) error {
	monitor.mutex.Lock()
	defer monitor.mutex.Unlock()

	existing := monitor.attachments[virtualMachineID]
	if existing == nil {
		return nil
	}
	delete(monitor.attachments, virtualMachineID)
	delete(monitor.byUserID, existing.target.UserID)
	return errors.Join(existing.closeHook(), monitor.hooks.clear(existing.target.UserID))
}

// Sample returns the current monotonic idle duration and packet sequence.
func (monitor *Monitor) Sample(target Target) (Sample, error) {
	monitor.mutex.Lock()
	defer monitor.mutex.Unlock()

	existing := monitor.attachments[target.VirtualMachineID]
	if existing == nil || existing.target != target || !monitor.loaded {
		return Sample{}, fmt.Errorf("sample traffic for VM %s: %w", target.VirtualMachineID, ErrNotFound)
	}
	packetTime, found, err := monitor.hooks.lastPacket(target.UserID)
	if err != nil {
		return Sample{}, fmt.Errorf("sample traffic for VM %s: %w", target.VirtualMachineID, err)
	}
	now, err := monitor.clock()
	if err != nil {
		return Sample{}, fmt.Errorf("read traffic clock for VM %s: %w", target.VirtualMachineID, err)
	}
	lastActivity := existing.baseline
	if found {
		lastActivity = packetTime
	}
	if now < lastActivity {
		return Sample{PacketSequence: packetTime}, nil
	}
	return Sample{IdleFor: time.Duration(now - lastActivity), PacketSequence: packetTime}, nil
}

// GetTrafficCounters returns cumulative received and sent bytes and packets for one target.
func (monitor *Monitor) GetTrafficCounters(target Target) (received, sent TrafficCounters, err error) {
	monitor.mutex.Lock()
	defer monitor.mutex.Unlock()

	existing := monitor.attachments[target.VirtualMachineID]
	if existing == nil || existing.target != target || !monitor.loaded {
		return TrafficCounters{}, TrafficCounters{}, fmt.Errorf("read traffic counters for VM %s: %w", target.VirtualMachineID, ErrNotFound)
	}
	received, sent, err = monitor.hooks.readTrafficCounters(target.UserID)
	if err != nil {
		return TrafficCounters{}, TrafficCounters{}, fmt.Errorf("read traffic counters for VM %s: %w", target.VirtualMachineID, err)
	}
	return received, sent, nil
}

// StartWatching enables traffic events for one target.
func (monitor *Monitor) StartWatching(target Target) error {
	return monitor.setWatching(target, true)
}

// StopWatching disables traffic events for one target.
func (monitor *Monitor) StopWatching(target Target) error {
	return monitor.setWatching(target, false)
}

// setWatching changes whether one attachment emits packet events.
// Events returns packet events for watched targets.
func (monitor *Monitor) Events() <-chan Event {
	return monitor.events
}

// Close releases all monitor resources.
func (monitor *Monitor) Close() error {
	monitor.closeOnce.Do(func() {
		close(monitor.closing)
		monitor.mutex.Lock()
		monitor.closed = true
		loaded := monitor.loaded
		workerDone := monitor.workerDone
		if !loaded {
			close(monitor.events)
		}
		monitor.mutex.Unlock()

		if loaded {
			monitor.closeError = errors.Join(monitor.closeError, monitor.hooks.closeEventReader())
			<-workerDone
		}

		monitor.mutex.Lock()
		defer monitor.mutex.Unlock()
		for _, existing := range monitor.attachments {
			monitor.closeError = errors.Join(monitor.closeError, existing.closeHook())
		}
		monitor.attachments = make(map[string]*attachment)
		monitor.byUserID = make(map[uint32]string)
		if loaded {
			monitor.closeError = errors.Join(monitor.closeError, monitor.hooks.closeMaps())
		}
	})
	return monitor.closeError
}

// attachmentMatches reports whether an existing attachment still targets the same TAP.
func (monitor *Monitor) attachmentMatches(existing *attachment, request AttachmentRequest) bool {
	if existing.target != request.Target || existing.namespacePath != request.NamespacePath || existing.interfaceName != request.InterfaceName {
		return false
	}
	index, err := monitor.hooks.interfaceIndex(request.NamespacePath, request.InterfaceName)
	return err == nil && index == existing.interfaceIndex
}

// setWatching changes whether one attachment emits packet events.
func (monitor *Monitor) setWatching(target Target, watching bool) error {
	monitor.mutex.Lock()
	defer monitor.mutex.Unlock()

	existing := monitor.attachments[target.VirtualMachineID]
	if existing == nil || existing.target != target || !monitor.loaded {
		if !watching {
			return nil
		}
		return fmt.Errorf("watch traffic for VM %s: %w", target.VirtualMachineID, ErrNotFound)
	}
	if err := monitor.hooks.setWatching(target.UserID, watching); err != nil {
		return fmt.Errorf("set traffic watch for VM %s: %w", target.VirtualMachineID, err)
	}
	return nil
}

// load opens shared hooks and starts the event reader.
func (monitor *Monitor) load() error {
	if monitor.loaded {
		return nil
	}
	if err := monitor.hooks.open(monitor.capacity); err != nil {
		return fmt.Errorf("open traffic hooks: %w", err)
	}
	monitor.loaded = true
	monitor.workerDone = make(chan struct{})
	go monitor.readEvents()
	return nil
}

// readEvents forwards kernel events until the reader or monitor closes.
func (monitor *Monitor) readEvents() {
	defer close(monitor.workerDone)
	defer close(monitor.events)

	for {
		userID, err := monitor.hooks.readEvent()
		if errors.Is(err, errEventReaderClosed) {
			return
		}
		if err != nil {
			monitor.logger.Warn("traffic event read failed", "error", err)
			select {
			case <-time.After(readRetryDelay):
			case <-monitor.closing:
				return
			}
			continue
		}
		monitor.publishEvent(userID)
	}
}

// publishEvent maps a kernel user ID to its virtual machine and emits an event.
func (monitor *Monitor) publishEvent(userID uint32) {
	monitor.mutex.Lock()
	virtualMachineID, found := monitor.byUserID[userID]
	monitor.mutex.Unlock()
	if !found {
		return
	}

	select {
	case monitor.events <- Event{Target: Target{VirtualMachineID: virtualMachineID, UserID: userID}}:
	case <-monitor.closing:
	}
}

// readMonotonicNanoseconds returns the kernel monotonic clock in nanoseconds.
func readMonotonicNanoseconds() (uint64, error) {
	var now unix.Timespec
	if err := unix.ClockGettime(unix.CLOCK_MONOTONIC, &now); err != nil {
		return 0, err
	}
	return uint64(now.Sec)*uint64(time.Second) + uint64(now.Nsec), nil
}
