// Package metrics collects and retains host and VM measurements on disk.
package metrics

import (
	"bufio"
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/frappe/atlas/metal/internal/host"
	"github.com/frappe/atlas/metal/internal/vm"
)

const Retention = 24 * time.Hour
const hourLayout = "20060102T15"

// Record is one timestamped measurement of a host or VM.
type Record struct {
	Timestamp time.Time      `json:"timestamp"`
	VM        *vm.Metrics    `json:"vm,omitempty"`
	Host      *host.Capacity `json:"host,omitempty"`
}

// Store serializes appends, retention cleanup, and history reads.
type Store struct {
	directory string
	mutex     sync.RWMutex
}

func NewStore(directory string) (*Store, error) {
	if err := os.MkdirAll(directory, 0700); err != nil {
		return nil, err
	}
	return &Store{directory: directory}, nil
}

func (store *Store) resourceDirectory(resource string) string {
	return filepath.Join(store.directory, hex.EncodeToString([]byte(resource)))
}

func (store *Store) Append(resource string, record Record) error {
	store.mutex.Lock()
	defer store.mutex.Unlock()
	directory := store.resourceDirectory(resource)
	if err := os.MkdirAll(directory, 0700); err != nil {
		return err
	}
	data, err := json.Marshal(record)
	if err != nil {
		return err
	}
	path := filepath.Join(directory, record.Timestamp.UTC().Format(hourLayout)+".jsonl")
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer file.Close()
	size, err := file.Seek(0, io.SeekEnd)
	if err != nil {
		return err
	}
	if size > 0 {
		last := make([]byte, 1)
		if _, err := file.ReadAt(last, size-1); err != nil {
			return err
		}
		if last[0] != '\n' {
			// A process interruption can leave an unfinished final record.
			existing, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			size = int64(bytes.LastIndexByte(existing, '\n') + 1)
			if err := file.Truncate(size); err != nil {
				return err
			}
			if _, err := file.Seek(size, io.SeekStart); err != nil {
				return err
			}
		}
	}
	if _, err := file.Write(append(data, '\n')); err != nil {
		return err
	}
	return file.Sync()
}

func readRecords(ctx context.Context, path string) ([]Record, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	records := []Record{}
	reader := bufio.NewReader(file)
	for {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		line, err := reader.ReadBytes('\n')
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, err
		}
		var record Record
		if err := json.Unmarshal(line, &record); err != nil {
			return nil, fmt.Errorf("decode %s: %w", path, err)
		}
		records = append(records, record)
	}
	return records, nil
}

// History returns records in [start, end), in timestamp order.
func (store *Store) History(ctx context.Context, resource string, start, end time.Time) ([]Record, error) {
	store.mutex.RLock()
	defer store.mutex.RUnlock()
	entries, err := os.ReadDir(store.resourceDirectory(resource))
	if errors.Is(err, os.ErrNotExist) {
		return []Record{}, nil
	}
	if err != nil {
		return nil, err
	}
	result := []Record{}
	for _, entry := range entries {
		hour, err := time.Parse(hourLayout+".jsonl", entry.Name())
		if err != nil || !hour.Before(end) || !hour.Add(time.Hour).After(start) {
			continue
		}
		records, err := readRecords(ctx, filepath.Join(store.resourceDirectory(resource), entry.Name()))
		if err != nil {
			return nil, err
		}
		for _, record := range records {
			if !record.Timestamp.Before(start) && record.Timestamp.Before(end) {
				result = append(result, record)
			}
		}
	}
	slices.SortStableFunc(result, func(a, b Record) int { return a.Timestamp.Compare(b.Timestamp) })
	return result, nil
}

// Prune removes expired records, including the expired part of the oldest hour.
func (store *Store) Prune(ctx context.Context, now time.Time) error {
	store.mutex.Lock()
	defer store.mutex.Unlock()
	cutoff := now.Add(-Retention)
	resources, err := os.ReadDir(store.directory)
	if err != nil {
		return err
	}
	for _, resource := range resources {
		if !resource.IsDir() {
			continue
		}
		directory := filepath.Join(store.directory, resource.Name())
		entries, err := os.ReadDir(directory)
		if err != nil {
			return err
		}
		for _, entry := range entries {
			if strings.HasPrefix(entry.Name(), ".retention-") {
				if err := os.Remove(filepath.Join(directory, entry.Name())); err != nil {
					return err
				}
				continue
			}
			if err := ctx.Err(); err != nil {
				return err
			}
			hour, err := time.Parse(hourLayout+".jsonl", entry.Name())
			if err != nil || !hour.Before(cutoff) {
				continue
			}
			path := filepath.Join(directory, entry.Name())
			if !hour.Add(time.Hour).After(cutoff) {
				if err := os.Remove(path); err != nil {
					return err
				}
				continue
			}
			records, err := readRecords(ctx, path)
			if err != nil {
				return err
			}
			retained := records[:0]
			for _, record := range records {
				if !record.Timestamp.Before(cutoff) {
					retained = append(retained, record)
				}
			}
			if len(retained) == len(records) {
				continue
			}
			if len(retained) == 0 {
				if err := os.Remove(path); err != nil {
					return err
				}
			} else if err := replaceRecords(path, retained); err != nil {
				return err
			}
		}
		remaining, err := os.ReadDir(directory)
		if err != nil {
			return err
		}
		if len(remaining) == 0 {
			if err := os.Remove(directory); err != nil {
				return err
			}
		}
	}
	return nil
}

func replaceRecords(path string, records []Record) error {
	file, err := os.CreateTemp(filepath.Dir(path), ".retention-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	defer file.Close()
	encoder := json.NewEncoder(file)
	for _, record := range records {
		if err := encoder.Encode(record); err != nil {
			return err
		}
	}
	if err := file.Sync(); err != nil {
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	return os.Rename(file.Name(), path)
}
