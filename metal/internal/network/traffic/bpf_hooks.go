package traffic

import (
	"bytes"
	_ "embed"
	"encoding/binary"
	"errors"
	"fmt"
	"net"

	"github.com/cilium/ebpf"
	"github.com/cilium/ebpf/link"
	"github.com/cilium/ebpf/ringbuf"
)

//go:embed track_traffic.o
var trafficBPFObject []byte

type trafficPrograms struct {
	GuestReceived *ebpf.Program `ebpf:"track_guest_received"`
	GuestSent     *ebpf.Program `ebpf:"track_guest_sent"`
}

// TrafficCounters holds cumulative byte and packet counts for one direction.
type TrafficCounters struct {
	Bytes   uint64
	Packets uint64
}

// SentCounters holds cumulative guest-sent traffic and packet categories.
type SentCounters struct {
	TrafficCounters
	ICMPPackets   uint64
	UDPPackets    uint64
	TCPSYNPackets uint64
	TCPRSTPackets uint64
}

type bpfHooks struct {
	activity         *ebpf.Map
	watch            *ebpf.Map
	events           *ebpf.Map
	receivedCounters *ebpf.Map
	sentCounters     *ebpf.Map
	reader           *ringbuf.Reader
}

// open creates shared eBPF maps and the event reader.
func (hooks *bpfHooks) open(capacity uint32) error {
	specification, err := loadCollectionSpec()
	if err != nil {
		return err
	}

	hooks.activity, err = createHashMap(specification, "activity_by_user_id", capacity)
	if err != nil {
		return err
	}
	hooks.watch, err = createHashMap(specification, "watch_by_user_id", capacity)
	if err != nil {
		return errors.Join(err, hooks.activity.Close())
	}
	hooks.receivedCounters, err = createHashMap(specification, "rx_counters_by_user_id", capacity)
	if err != nil {
		return errors.Join(err, hooks.activity.Close(), hooks.watch.Close())
	}
	hooks.sentCounters, err = createHashMap(specification, "tx_counters_by_user_id", capacity)
	if err != nil {
		return errors.Join(err, hooks.activity.Close(), hooks.watch.Close(), hooks.receivedCounters.Close())
	}
	hooks.events, err = ebpf.NewMap(specification.Maps["traffic_events"].Copy())
	if err != nil {
		return errors.Join(err, hooks.activity.Close(), hooks.watch.Close(), hooks.receivedCounters.Close(), hooks.sentCounters.Close())
	}
	hooks.reader, err = ringbuf.NewReader(hooks.events)
	if err != nil {
		return errors.Join(err, hooks.closeMaps())
	}
	return nil
}

// attach loads both programs and attaches them to the TAP egress (guest-received)
// and ingress (guest-sent) hooks.
func (hooks *bpfHooks) attach(userID uint32, namespacePath, interfaceName string) (int, func() error, error) {
	programs, err := hooks.loadPrograms(userID)
	if err != nil {
		return 0, nil, err
	}

	var interfaceIndex int
	var egress, ingress link.Link
	err = withNetworkNamespace(namespacePath, func() error {
		device, err := net.InterfaceByName(interfaceName)
		if err != nil {
			return fmt.Errorf("resolve %s: %w", interfaceName, err)
		}
		interfaceIndex = device.Index
		egress, err = link.AttachTCX(link.TCXOptions{
			Program:   programs.GuestReceived,
			Attach:    ebpf.AttachTCXEgress,
			Interface: interfaceIndex,
		})
		if err != nil {
			return fmt.Errorf("attach TCX egress: %w", err)
		}
		ingress, err = link.AttachTCX(link.TCXOptions{
			Program:   programs.GuestSent,
			Attach:    ebpf.AttachTCXIngress,
			Interface: interfaceIndex,
		})
		if err != nil {
			return fmt.Errorf("attach TCX ingress: %w", err)
		}
		return nil
	})
	if err != nil {
		return 0, nil, errors.Join(err, closeIfNotNil(egress), programs.GuestReceived.Close(), programs.GuestSent.Close())
	}

	closeHook := func() error {
		return errors.Join(egress.Close(), ingress.Close(), programs.GuestReceived.Close(), programs.GuestSent.Close())
	}
	return interfaceIndex, closeHook, nil
}

func closeIfNotNil(hook link.Link) error {
	if hook == nil {
		return nil
	}
	return hook.Close()
}

func (hooks *bpfHooks) loadPrograms(userID uint32) (trafficPrograms, error) {
	specification, err := loadCollectionSpec()
	if err != nil {
		return trafficPrograms{}, err
	}
	specification.Maps["activity_by_user_id"].MaxEntries = hooks.activity.MaxEntries()
	specification.Maps["watch_by_user_id"].MaxEntries = hooks.watch.MaxEntries()
	specification.Maps["rx_counters_by_user_id"].MaxEntries = hooks.receivedCounters.MaxEntries()
	specification.Maps["tx_counters_by_user_id"].MaxEntries = hooks.sentCounters.MaxEntries()
	if err := specification.Variables["virtual_machine_user_id"].Set(userID); err != nil {
		return trafficPrograms{}, fmt.Errorf("set user ID constant: %w", err)
	}

	var programs trafficPrograms
	err = specification.LoadAndAssign(&programs, &ebpf.CollectionOptions{
		MapReplacements: map[string]*ebpf.Map{
			"activity_by_user_id":    hooks.activity,
			"watch_by_user_id":       hooks.watch,
			"rx_counters_by_user_id": hooks.receivedCounters,
			"tx_counters_by_user_id": hooks.sentCounters,
			"traffic_events":         hooks.events,
		},
	})
	return programs, err
}

// interfaceIndex resolves a TAP index inside its network namespace.
func (hooks *bpfHooks) interfaceIndex(namespacePath, interfaceName string) (int, error) {
	var interfaceIndex int
	err := withNetworkNamespace(namespacePath, func() error {
		device, err := net.InterfaceByName(interfaceName)
		if err != nil {
			return fmt.Errorf("resolve %s: %w", interfaceName, err)
		}
		interfaceIndex = device.Index
		return nil
	})
	return interfaceIndex, err
}

// lastPacket reads the last packet timestamp for one user ID.
func (hooks *bpfHooks) lastPacket(userID uint32) (uint64, bool, error) {
	var nanoseconds uint64
	err := hooks.activity.Lookup(userID, &nanoseconds)
	if errors.Is(err, ebpf.ErrKeyNotExist) {
		return 0, false, nil
	}
	return nanoseconds, err == nil, err
}

// setWatching enables or disables one-shot packet events.
func (hooks *bpfHooks) setWatching(userID uint32, watching bool) error {
	state := uint32(0)
	if watching {
		state = 1
	}
	return hooks.watch.Update(userID, state, ebpf.UpdateAny)
}

func (hooks *bpfHooks) clearIdle(userID uint32) error {
	return errors.Join(
		deleteMapValue(hooks.activity, userID),
		deleteMapValue(hooks.watch, userID),
	)
}

func (hooks *bpfHooks) clear(userID uint32) error {
	return errors.Join(
		hooks.clearIdle(userID),
		deleteMapValue(hooks.receivedCounters, userID),
		deleteMapValue(hooks.sentCounters, userID),
	)
}

// readTrafficCounters reads both directions. An absent key has zero counters.
func (hooks *bpfHooks) readTrafficCounters(userID uint32) (TrafficCounters, SentCounters, error) {
	var received TrafficCounters
	if err := hooks.receivedCounters.Lookup(userID, &received); err != nil && !errors.Is(err, ebpf.ErrKeyNotExist) {
		return TrafficCounters{}, SentCounters{}, err
	}
	var sent SentCounters
	if err := hooks.sentCounters.Lookup(userID, &sent); err != nil && !errors.Is(err, ebpf.ErrKeyNotExist) {
		return TrafficCounters{}, SentCounters{}, err
	}
	return received, sent, nil
}

// deleteMapValue removes one map value and accepts an absent value.
func deleteMapValue(kernelMap *ebpf.Map, key uint32) error {
	if err := kernelMap.Delete(key); err != nil && !errors.Is(err, ebpf.ErrKeyNotExist) {
		return err
	}
	return nil
}

// readEvent reads one user ID from the kernel ring buffer.
func (hooks *bpfHooks) readEvent() (uint32, error) {
	record, err := hooks.reader.Read()
	if errors.Is(err, ringbuf.ErrClosed) {
		return 0, errEventReaderClosed
	}
	if err != nil {
		return 0, err
	}
	if len(record.RawSample) < 4 {
		return 0, fmt.Errorf("traffic event sample is %d bytes, want at least 4", len(record.RawSample))
	}
	return binary.LittleEndian.Uint32(record.RawSample[0:4]), nil
}

// closeEventReader stops the ring-buffer reader.
func (hooks *bpfHooks) closeEventReader() error {
	return hooks.reader.Close()
}

// closeMaps releases all shared eBPF maps.
func (hooks *bpfHooks) closeMaps() error {
	var closeErrors []error
	for _, kernelMap := range []*ebpf.Map{hooks.activity, hooks.watch, hooks.receivedCounters, hooks.sentCounters, hooks.events} {
		if kernelMap != nil {
			closeErrors = append(closeErrors, kernelMap.Close())
		}
	}
	return errors.Join(closeErrors...)
}

// loadCollectionSpec loads the embedded eBPF object.
func loadCollectionSpec() (*ebpf.CollectionSpec, error) {
	specification, err := ebpf.LoadCollectionSpecFromReader(bytes.NewReader(trafficBPFObject))
	if err != nil {
		return nil, fmt.Errorf("load traffic eBPF object: %w", err)
	}
	return specification, nil
}

// createHashMap creates a hash map with the requested capacity.
func createHashMap(specification *ebpf.CollectionSpec, name string, capacity uint32) (*ebpf.Map, error) {
	mapSpecification := specification.Maps[name].Copy()
	mapSpecification.MaxEntries = capacity
	return ebpf.NewMap(mapSpecification)
}
