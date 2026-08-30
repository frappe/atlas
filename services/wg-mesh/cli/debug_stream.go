package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"net/netip"
	"os"
	"sort"
	"time"

	"github.com/cilium/ebpf/ringbuf"
)

type debugPair struct {
	source      netip.Addr
	destination netip.Addr
}

type debugPairStats struct {
	accepted uint64
	dropped  uint64
	redirect uint64
}

func dumpDebug(filter debugFilter) error {
	reader, closeReader, err := openDebugReader()
	if err != nil {
		return err
	}
	defer closeReader()

	// Report boot-time timestamps relative to the first printed event.
	var first uint64
	for {
		event, err := readDebugEvent(reader)
		if err != nil {
			return err
		}
		if !filter.matches(event) {
			continue
		}
		if first == 0 {
			first = event.Timestamp
		}
		printDebugEvent(event, first)
	}
}

func topDebug(filter debugFilter) error {
	reader, closeReader, err := openDebugReader()
	if err != nil {
		return err
	}
	defer closeReader()

	pairs := make(map[debugPair]debugPairStats)
	printDebugTop(pairs)
	refresher := time.NewTicker(5 * time.Second)
	defer refresher.Stop()
	for {
		reader.SetDeadline(time.Now().Add(100 * time.Millisecond))
		event, err := readDebugEvent(reader)
		if err != nil && !errors.Is(err, os.ErrDeadlineExceeded) {
			return err
		}
		if err == nil && event.Operation == 0 && filter.matches(event) {
			updateDebugPair(pairs, event)
		}
		select {
		case <-refresher.C:
			printDebugTop(pairs)
		default:
		}
	}
}

func openDebugReader() (*ringbuf.Reader, func(), error) {
	events, err := openMap("debug_events")
	if err != nil {
		return nil, func() {}, err
	}
	reader, err := ringbuf.NewReader(events)
	if err != nil {
		events.Close()
		return nil, func() {}, err
	}
	return reader, func() { reader.Close(); events.Close() }, nil
}

func readDebugEvent(reader *ringbuf.Reader) (debugEvent, error) {
	record, err := reader.Read()
	if err != nil {
		return debugEvent{}, err
	}
	var event debugEvent
	err = binary.Read(bytes.NewReader(record.RawSample), binary.NativeEndian, &event)
	return event, err
}

func updateDebugPair(pairs map[debugPair]debugPairStats, event debugEvent) {
	pair := debugPair{netip.AddrFrom16(event.Source), netip.AddrFrom16(event.Destination)}
	stats := pairs[pair]
	switch event.Verdict {
	case 0:
		stats.accepted++
	case 1:
		stats.dropped++
	case 2:
		stats.redirect++
	}
	pairs[pair] = stats
}

func printDebugTop(pairs map[debugPair]debugPairStats) {
	type row struct {
		pair  debugPair
		stats debugPairStats
	}
	rows := make([]row, 0, len(pairs))
	for pair, stats := range pairs {
		rows = append(rows, row{pair, stats})
	}
	sort.Slice(rows, func(left, right int) bool {
		leftTotal := rows[left].stats.accepted + rows[left].stats.dropped + rows[left].stats.redirect
		rightTotal := rows[right].stats.accepted + rows[right].stats.dropped + rows[right].stats.redirect
		return leftTotal > rightTotal
	})
	fmt.Print("\033[2J\033[H")
	fmt.Println("src\tdst\taccept\tdrop\tredirect")
	for _, row := range rows {
		fmt.Printf("%s\t%s\t%d\t%d\t%d\n", row.pair.source, row.pair.destination, row.stats.accepted, row.stats.dropped, row.stats.redirect)
	}
}

func printDebugEvent(event debugEvent, first uint64) {
	elapsed := time.Duration(event.Timestamp - first).Seconds()
	if event.Operation != 0 {
		fmt.Printf("%8.3f %-9s %s %s vm=%s host=%s tenant=%d\n", elapsed, hookName(event.Hook), directionName(event.Direction), operationName(event.Operation), netip.AddrFrom16(event.VM), netip.AddrFrom16(event.Host), binary.BigEndian.Uint32(event.Tenant[:]))
		return
	}
	fmt.Printf("%8.3f %-9s %-8s src=%s dst=%s tenant=%d\n", elapsed, hookName(event.Hook), verdictName(event.Verdict), netip.AddrFrom16(event.Source), netip.AddrFrom16(event.Destination), binary.BigEndian.Uint32(event.Tenant[:]))
}
