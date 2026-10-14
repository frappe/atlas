package system

import (
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// Captured from a file-backed mirror on ZFS 2.4 after one side was corrupted
// and scrubbed.
const repairedPoolStatus = `  pool: test
 state: ONLINE
status: One or more devices has experienced an unrecoverable error.  An
	attempt was made to correct the error.  Applications are unaffected.
action: Determine if the device needs to be replaced, and clear the errors
	using 'zpool clear' or replace the device with 'zpool replace'.
   see: https://openzfs.github.io/openzfs-docs/msg/ZFS-8000-9P
  scan: scrub repaired 10.3M in 00:00:00 with 0 errors on Wed Oct 14 04:26:30 2026
config:

	NAME            STATE     READ WRITE CKSUM  SLOW
	test            ONLINE       0     0     0     0
	  mirror-0      ONLINE       0     0     0     0
	    /tmp/a.img  ONLINE       0     0     0     0
	    /tmp/b.img  ONLINE       0     0   130     4

errors: No known data errors
`

// Captured after the same block was corrupted on both sides.
const corruptPoolStatus = `  pool: test
 state: ONLINE
status: One or more devices has experienced an error resulting in data
	corruption.  Applications may be affected.
  scan: scrub repaired 0B in 00:00:00 with 1 errors on Wed Oct 14 04:27:27 2026
config:

	NAME            STATE     READ WRITE CKSUM  SLOW
	test            ONLINE       0     0     0     0
	  mirror-0      ONLINE       0     0     0     0
	    /tmp/a.img  ONLINE       0     0     2     0
	    /tmp/b.img  OFFLINE      0     0     2     0

errors: 1 data errors, use '-v' for a list
`

const runningPoolStatus = `  pool: test
 state: ONLINE
  scan: resilver in progress since Wed Oct 14 04:26:46 2026
	1.20G / 50.1G scanned at 300M/s, 600M / 50.1G issued at 150M/s
	1.20G resilvered, 4.88% done, 00:05:30 to go
config:

	NAME        STATE     READ WRITE CKSUM  SLOW
	test        ONLINE       0     0     0     -
	  sda       ONLINE       0     0     0     0

errors: No known data errors
`

func TestPoolStatusReportsARepairingScrubAndDeviceErrors(t *testing.T) {
	samples, err := parsePoolStatus("test", repairedPoolStatus, time.UTC)
	if err != nil {
		t.Fatal(err)
	}

	scrub := telemetry.Labels{"pool": "test", "function": "scrub"}
	if repaired := valueOf(t, samples, "system_zfs_scan_repaired_bytes", scrub); repaired != 10.3*1024*1024 {
		t.Fatalf("repaired = %v, want 10.3 MiB", repaired)
	}
	finished := time.Date(2026, time.October, 14, 4, 26, 30, 0, time.UTC)
	if end := valueOf(t, samples, "system_zfs_scan_end_timestamp_seconds", scrub); end != float64(finished.Unix()) {
		t.Fatalf("end = %v, want %v", end, finished.Unix())
	}
	if checksum := valueOf(t, samples, "system_zfs_device_errors_maximum", telemetry.Labels{"pool": "test", "kind": "checksum"}); checksum != 130 {
		t.Fatalf("checksum errors = %v, want 130", checksum)
	}
	if slow := valueOf(t, samples, "system_zfs_device_slow_ios", telemetry.Labels{"pool": "test", "device": "/tmp/b.img"}); slow != 4 {
		t.Fatalf("slow I/Os = %v, want 4", slow)
	}
	if hasMetricWithLabel(samples, "device", "attempt") || hasMetricWithLabel(samples, "device", "using") {
		t.Fatal("a status message line was read as a device")
	}
	if dataErrors := valueOf(t, samples, "system_zfs_pool_data_errors", telemetry.Labels{"pool": "test"}); dataErrors != 0 {
		t.Fatalf("data errors = %v, want 0", dataErrors)
	}
}

func TestPoolStatusReportsDataThatNoCopyCouldRepair(t *testing.T) {
	samples, err := parsePoolStatus("test", corruptPoolStatus, time.UTC)
	if err != nil {
		t.Fatal(err)
	}

	if dataErrors := valueOf(t, samples, "system_zfs_pool_data_errors", telemetry.Labels{"pool": "test"}); dataErrors != 1 {
		t.Fatalf("data errors = %v, want 1", dataErrors)
	}
	if scanErrors := valueOf(t, samples, "system_zfs_scan_errors", telemetry.Labels{"pool": "test", "function": "scrub"}); scanErrors != 1 {
		t.Fatalf("scan errors = %v, want 1", scanErrors)
	}
	valueOf(t, samples, "system_zfs_device_state", telemetry.Labels{"pool": "test", "device": "/tmp/b.img", "state": "offline"})
}

func TestPoolStatusReportsResilverProgress(t *testing.T) {
	samples, err := parsePoolStatus("test", runningPoolStatus, time.UTC)
	if err != nil {
		t.Fatal(err)
	}

	valueOf(t, samples, "system_zfs_scan_state", telemetry.Labels{"pool": "test", "function": "resilver", "state": "running"})
	if progress := valueOf(t, samples, "system_zfs_scan_progress_percent", telemetry.Labels{"pool": "test", "function": "resilver"}); progress != 4.88 {
		t.Fatalf("progress = %v, want 4.88", progress)
	}
}
