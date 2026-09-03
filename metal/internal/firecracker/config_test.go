package firecracker

import (
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func TestAtomicWriteFilePublishesCompleteContent(t *testing.T) {
	directory := t.TempDir()
	path := filepath.Join(directory, "config.json")
	contents := [][]byte{
		[]byte(`{"id":"first"}`),
		[]byte(`{"id":"second","value":"longer"}`),
	}

	var writers sync.WaitGroup
	for _, content := range contents {
		writers.Add(1)
		go func() {
			defer writers.Done()
			if err := atomicWriteFile(path, content, 0o640); err != nil {
				t.Errorf("write config: %v", err)
			}
		}()
	}
	writers.Wait()

	actual, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(actual) != string(contents[0]) && string(actual) != string(contents[1]) {
		t.Fatalf("config content = %q", actual)
	}
	matches, err := filepath.Glob(filepath.Join(directory, ".config.json-*"))
	if err != nil {
		t.Fatal(err)
	}
	if len(matches) != 0 {
		t.Fatalf("temporary files remain: %v", matches)
	}
}
