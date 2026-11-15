package network

import (
	"testing"
	"time"
)

func TestMeshPeerSamplesCountOnlyRecentHandshakes(t *testing.T) {
	now := time.Unix(1_000_000, 0)
	peers := []WireGuardPeer{{Node: "recent", PublicKey: "key-1"}, {Node: "old", PublicKey: "key-2"}, {Node: "never", PublicKey: "key-3"}, {Node: "absent", PublicKey: "key-4"}}
	handshakes := "key-1\t999900\nkey-2\t999700\nkey-3\t0\natlas-key\t999990\n"

	samples := meshPeerSamples(peers, handshakes, now)

	want := map[string]float64{"recent": 1, "old": 0, "never": 0, "absent": 0}
	if len(samples) != len(want) {
		t.Fatalf("got %d samples, want %d", len(samples), len(want))
	}
	for _, sample := range samples {
		if sample.Metric != "metal_network_mesh_peer_connected" || sample.Value != want[sample.Labels["peer"]] {
			t.Errorf("peer %q: got %s = %v, want %v", sample.Labels["peer"], sample.Metric, sample.Value, want[sample.Labels["peer"]])
		}
	}
}
