package metrics

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"
)

const Retention = 7 * 24 * time.Hour
const dayLayout = "20060102"
const metricsDirectory = "metrics"

// Record is one timestamped VM measurement.
type Record struct {
	Timestamp time.Time `json:"timestamp"`
	Metrics   Sample    `json:"metrics"`
}

func (record Record) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Timestamp int64  `json:"timestamp"`
		Metrics   Sample `json:"metrics"`
	}{Timestamp: record.Timestamp.Unix(), Metrics: record.Metrics})
}

func (record *Record) UnmarshalJSON(data []byte) error {
	var stored struct {
		Timestamp int64  `json:"timestamp"`
		Metrics   Sample `json:"metrics"`
	}
	if err := json.Unmarshal(data, &stored); err != nil {
		return err
	}
	record.Timestamp = time.Unix(stored.Timestamp, 0).UTC()
	record.Metrics = stored.Metrics
	return nil
}

// Store keeps each VM's daily metrics inside its existing records directory.
type Store struct {
	directory string
}

// NewStore opens the VM records directory for metrics storage.
func NewStore(directory string) (*Store, error) {
	if err := os.MkdirAll(directory, 0700); err != nil {
		return nil, err
	}
	return &Store{directory: directory}, nil
}

func (store *Store) metricsPath(identifier string) (string, error) {
	if !validIdentifier(identifier) {
		return "", fmt.Errorf("invalid virtual machine identifier %q", identifier)
	}
	return filepath.Join(store.directory, identifier, metricsDirectory), nil
}

// Append writes one sample to its UTC day without recreating a deleted VM.
func (store *Store) Append(identifier string, record Record) error {
	directory, err := store.metricsPath(identifier)
	if err != nil {
		return err
	}
	if err := os.Mkdir(directory, 0700); err != nil && !errors.Is(err, fs.ErrExist) {
		return err
	}
	data, err := json.Marshal(record)
	if err != nil {
		return err
	}
	path := filepath.Join(directory, record.Timestamp.UTC().Format(dayLayout)+".jsonl")
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|os.O_APPEND, 0600)
	if err != nil {
		return err
	}
	defer file.Close()
	if err := removePartialTail(file); err != nil {
		return err
	}
	_, err = file.Write(append(data, '\n'))
	return err
}

func removePartialTail(file *os.File) error {
	size, err := file.Seek(0, io.SeekEnd)
	if err != nil || size == 0 {
		return err
	}
	last := make([]byte, 1)
	if _, err := file.ReadAt(last, size-1); err != nil {
		return err
	}
	if last[0] == '\n' {
		return nil
	}
	// A stopped write can leave one incomplete record at the end of a day.
	data, err := os.ReadFile(file.Name())
	if err != nil {
		return err
	}
	return file.Truncate(int64(bytes.LastIndexByte(data, '\n') + 1))
}

func scanRecords(ctx context.Context, path string, start, end time.Time, accept func(Record)) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	reader := bufio.NewReader(file)
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		line, err := reader.ReadBytes('\n')
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return err
		}
		var record Record
		if err := json.Unmarshal(line, &record); err != nil {
			return fmt.Errorf("decode %s: %w", path, err)
		}
		if !record.Timestamp.Before(start) && record.Timestamp.Before(end) {
			accept(record)
		}
	}
	return nil
}

// History returns records in [start, end), in timestamp order.
func (store *Store) History(ctx context.Context, identifier string, start, end time.Time) ([]Record, error) {
	return store.history(ctx, identifier, start, end, 0)
}

// DownsampledHistory keeps the latest gauges and counters and averages disk rates in each UTC interval.
func (store *Store) DownsampledHistory(ctx context.Context, identifier string, start, end time.Time, interval time.Duration) ([]Record, error) {
	if interval < time.Second || interval%time.Second != 0 {
		return nil, errors.New("metrics interval must be whole seconds")
	}
	return store.history(ctx, identifier, start, end, interval)
}

func (store *Store) history(ctx context.Context, identifier string, start, end time.Time, interval time.Duration) ([]Record, error) {
	directory, err := store.metricsPath(identifier)
	if err != nil {
		return nil, err
	}
	if !start.Before(end) {
		return []Record{}, nil
	}
	if start.Before(end.Add(-Retention)) {
		start = end.Add(-Retention)
	}
	records := []Record{}
	type bucketTotals struct {
		position            int
		readBytesPerSecond  uint64
		writeBytesPerSecond uint64
		readMilliIOPS       uint64
		writeMilliIOPS      uint64
		count               uint64
	}
	buckets := make(map[int64]bucketTotals)
	accept := func(record Record) {
		if interval == 0 {
			records = append(records, record)
			return
		}

		bucket := record.Timestamp.Unix() / int64(interval/time.Second)
		totals, found := buckets[bucket]
		if !found {
			totals.position = len(records)
			records = append(records, record)
		} else if record.Timestamp.After(records[totals.position].Timestamp) {
			records[totals.position] = record
		}
		totals.readBytesPerSecond += record.Metrics.DiskReadBytesPerSecond
		totals.writeBytesPerSecond += record.Metrics.DiskWriteBytesPerSecond
		totals.readMilliIOPS += record.Metrics.DiskReadMilliIOPS
		totals.writeMilliIOPS += record.Metrics.DiskWriteMilliIOPS
		totals.count++
		buckets[bucket] = totals
	}
	for day := start.UTC().Truncate(24 * time.Hour); day.Before(end); day = day.Add(24 * time.Hour) {
		path := filepath.Join(directory, day.Format(dayLayout)+".jsonl")
		err := scanRecords(ctx, path, start, end, accept)
		if errors.Is(err, fs.ErrNotExist) {
			continue
		}
		if err != nil {
			return nil, err
		}
	}
	if interval > 0 {
		for _, totals := range buckets {
			position := totals.position
			records[position].Metrics.DiskReadBytesPerSecond = totals.readBytesPerSecond / totals.count
			records[position].Metrics.DiskWriteBytesPerSecond = totals.writeBytesPerSecond / totals.count
			records[position].Metrics.DiskReadMilliIOPS = totals.readMilliIOPS / totals.count
			records[position].Metrics.DiskWriteMilliIOPS = totals.writeMilliIOPS / totals.count
		}
	}
	slices.SortStableFunc(records, func(a, b Record) int { return a.Timestamp.Compare(b.Timestamp) })
	return records, nil
}

// Prune deletes complete daily files that ended more than seven days ago.
func (store *Store) Prune(ctx context.Context, now time.Time) error {
	entries, err := os.ReadDir(store.directory)
	if err != nil {
		return err
	}
	cutoff := now.Add(-Retention)
	for _, entry := range entries {
		if err := ctx.Err(); err != nil {
			return err
		}
		if !entry.IsDir() || !validIdentifier(entry.Name()) {
			continue
		}
		directory := filepath.Join(store.directory, entry.Name(), metricsDirectory)
		files, err := os.ReadDir(directory)
		if errors.Is(err, fs.ErrNotExist) {
			continue
		}
		if err != nil {
			return err
		}
		for _, file := range files {
			if file.IsDir() || !strings.HasSuffix(file.Name(), ".jsonl") {
				continue
			}
			day, err := time.Parse(dayLayout, strings.TrimSuffix(file.Name(), ".jsonl"))
			if err != nil || day.Add(24*time.Hour).After(cutoff) {
				continue
			}
			if err := os.Remove(filepath.Join(directory, file.Name())); err != nil && !errors.Is(err, fs.ErrNotExist) {
				return err
			}
		}
	}
	return nil
}

func validIdentifier(identifier string) bool {
	return identifier != "" && identifier != "." && filepath.Base(identifier) == identifier
}
