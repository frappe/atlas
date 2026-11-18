package network

import (
	"context"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// meshHandshakeWindow is how recent a handshake must be for a connected peer. Each peer sends a keepalive every 25
// seconds, and WireGuard makes a new handshake every 2 minutes while packets flow, so a working tunnel always has one.
const meshHandshakeWindow = 3 * time.Minute

// CollectTelemetry reports, for each mesh peer that Atlas sent, 1 when its tunnel had a handshake in the window.
func (manager *WireGuardManager) CollectTelemetry(ctx context.Context) ([]telemetry.Sample, error) {
	manager.mutex.Lock()
	peers, err := loadWireGuardPeers(manager.configuration.StatePath)
	manager.mutex.Unlock()
	if err != nil {
		return nil, err
	}

	output, err := manager.commands.Output(ctx, "wg", "show", manager.configuration.InterfaceName, "latest-handshakes")
	if err != nil {
		return nil, err
	}
	return meshPeerSamples(peers, output, time.Now()), nil
}

// meshPeerSamples reads `wg show latest-handshakes` lines: a public key and a Unix time, 0 before the first handshake.
func meshPeerSamples(peers []WireGuardPeer, handshakes string, now time.Time) []telemetry.Sample {
	latest := make(map[string]int64)
	for _, line := range strings.Split(handshakes, "\n") {
		fields := strings.Fields(line)
		if len(fields) != 2 {
			continue
		}
		if seconds, err := strconv.ParseInt(fields[1], 10, 64); err == nil {
			latest[fields[0]] = seconds
		}
	}

	samples := make([]telemetry.Sample, 0, len(peers))
	for _, peer := range peers {
		connected := 0.0
		if seconds := latest[peer.PublicKey]; seconds > 0 && now.Sub(time.Unix(seconds, 0)) < meshHandshakeWindow {
			connected = 1
		}
		samples = append(samples, telemetry.Sample{
			Metric: "metal_network_mesh_peer_connected", Value: connected, Labels: telemetry.Labels{"peer": peer.Node},
		})
	}
	return samples
}

// CollectPublicTelemetry reports 1 when the device of the IPv4 default route is up, and 0 without a default route.
func CollectPublicTelemetry(context.Context) ([]telemetry.Sample, error) {
	up := 0.0
	if name, err := defaultRouteInterface(); err == nil {
		state, err := os.ReadFile("/sys/class/net/" + name + "/operstate")
		if err != nil {
			return nil, err
		}
		// A device whose driver does not report its state, such as some virtual devices, shows unknown.
		if operationalState := strings.TrimSpace(string(state)); operationalState == "up" || operationalState == "unknown" {
			up = 1
		}
	}
	return []telemetry.Sample{{Metric: "metal_network_public_up", Value: up}}, nil
}
