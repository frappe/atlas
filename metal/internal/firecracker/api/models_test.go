package api

import "testing"

func TestBalloonStatisticsUsedMemoryBytes(t *testing.T) {
	gibibyte, mebibyte, zero := uint64(1<<30), uint64(1<<20), uint64(0)
	tests := []struct {
		name       string
		statistics BalloonStatistics
		want       uint64
		wantValid  bool
	}{
		{"complete", BalloonStatistics{TotalMemory: &gibibyte, AvailableMemory: &mebibyte}, gibibyte - mebibyte, true},
		{"available missing", BalloonStatistics{TotalMemory: &gibibyte}, 0, false},
		{"total missing", BalloonStatistics{AvailableMemory: &mebibyte}, 0, false},
		{"total zero", BalloonStatistics{TotalMemory: &zero, AvailableMemory: &zero}, 0, false},
		{"available above total", BalloonStatistics{TotalMemory: &mebibyte, AvailableMemory: &gibibyte}, 0, false},
	}
	for _, test := range tests {
		got, valid := test.statistics.UsedMemoryBytes()
		if got != test.want || valid != test.wantValid {
			t.Errorf("%s: got %d %t, want %d %t", test.name, got, valid, test.want, test.wantValid)
		}
	}
}
