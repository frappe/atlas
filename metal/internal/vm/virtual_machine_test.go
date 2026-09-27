package vm

import "testing"

func TestNetworkRequestTracksTrafficForRunningAndPausedVMs(t *testing.T) {
	cases := []struct {
		name         string
		state        State
		idle         int
		wantTrack    bool
		wantRequired bool
	}{
		{"running without idle shutdown", StateRunning, 0, true, false},
		{"running with idle shutdown", StateRunning, 300, true, true},
		{"stopped", StateStopped, 0, false, false},
		{"paused", StatePaused, 300, true, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			record := DesiredRecord{
				State: tc.state,
				Specification: Specification{
					SleepAfterIdleSeconds: tc.idle,
				},
			}
			request := networkRequest(record)
			if got := request.TrackTraffic; got != tc.wantTrack {
				t.Fatalf("TrackTraffic = %v, want %v", got, tc.wantTrack)
			}
			if got := request.RequireTrafficMonitor; got != tc.wantRequired {
				t.Fatalf("RequireTrafficMonitor = %v, want %v", got, tc.wantRequired)
			}
		})
	}
}
