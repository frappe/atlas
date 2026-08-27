package idalloc

import "testing"

func TestAllocate(t *testing.T) {
	r := Range{Min: 100, Max: 102}

	tests := []struct {
		name string
		used map[uint32]bool
		want uint32
		err  bool
	}{
		{"empty", nil, 100, false},
		{"skips used", map[uint32]bool{100: true, 101: true}, 102, false},
		{"lowest free in gap", map[uint32]bool{101: true}, 100, false},
		{"ignores out-of-range", map[uint32]bool{50: true, 100: true, 200: true}, 101, false},
		{"exhausted", map[uint32]bool{100: true, 101: true, 102: true}, 0, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := r.Allocate(tt.used)
			if tt.err {
				if err == nil {
					t.Fatalf("want error, got id %d", got)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != tt.want {
				t.Fatalf("got %d, want %d", got, tt.want)
			}
		})
	}
}
