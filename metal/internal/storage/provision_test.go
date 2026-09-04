package storage

import (
	"slices"
	"testing"
)

func TestParseCloneList(t *testing.T) {
	cases := []struct {
		name   string
		output string
		want   []string
	}{
		{"no snapshots", "", nil},
		{"only empty markers", "-\n-\n", nil},
		{"one clone", "metal/staging/snap-1\n-\n", []string{"metal/staging/snap-1"}},
		{
			"multiple clones on one snapshot",
			"metal/staging/a,metal/staging/b\n",
			[]string{"metal/staging/a", "metal/staging/b"},
		},
		{
			"clones across snapshots with blanks",
			"metal/staging/a\n\n-\nmetal/staging/b\n",
			[]string{"metal/staging/a", "metal/staging/b"},
		},
	}

	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if got := parseCloneList(testCase.output); !slices.Equal(got, testCase.want) {
				t.Errorf("parseCloneList(%q) = %v, want %v", testCase.output, got, testCase.want)
			}
		})
	}
}
