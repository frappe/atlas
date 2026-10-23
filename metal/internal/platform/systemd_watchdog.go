package platform

import (
	"context"
	"time"

	"github.com/coreos/go-systemd/v22/daemon"
)

// RunWatchdog pings the systemd watchdog at half its interval until ctx ends.
// It returns at once when the unit sets no WatchdogSec.
func RunWatchdog(ctx context.Context) {
	interval, err := daemon.SdWatchdogEnabled(false)
	if err != nil || interval == 0 {
		return
	}

	ticker := time.NewTicker(interval / 2)
	defer ticker.Stop()
	for {
		daemon.SdNotify(false, daemon.SdNotifyWatchdog)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}
