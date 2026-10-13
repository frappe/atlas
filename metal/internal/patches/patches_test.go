package patches

import (
	"log/slog"
	"testing"
)

func TestRunAppliesEachPatchOnce(t *testing.T) {
	runs := 0
	original := All
	All = []Patch{{Name: "count_runs", Apply: func(string) error { runs++; return nil }}}
	t.Cleanup(func() { All = original })

	baseDirectory := t.TempDir()
	for range 2 {
		if err := Run(baseDirectory, slog.New(slog.DiscardHandler)); err != nil {
			t.Fatal(err)
		}
	}

	if runs != 1 {
		t.Fatalf("the patch ran %d times, want once", runs)
	}
}
