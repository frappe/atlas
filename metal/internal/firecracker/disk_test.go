package firecracker

import (
	"testing"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

func TestDriveRateLimiterConvertsLimits(t *testing.T) {
	limiter := driveRateLimiter(vm.Disk{ThroughputMiBps: 40, IOPS: 2000})
	if limiter == nil {
		t.Fatal("limiter must be set")
	}
	if limiter.Bandwidth.Size != 40*1024*1024 || limiter.Bandwidth.RefillTime != 1000 {
		t.Fatalf("bandwidth = %+v", limiter.Bandwidth)
	}
	if limiter.Ops.Size != 2000 || limiter.Ops.RefillTime != 1000 {
		t.Fatalf("ops = %+v", limiter.Ops)
	}
}

// A temporary migration limit caps combined read and write throughput only.
func TestDriveRateLimiterThroughputOnly(t *testing.T) {
	limiter := driveRateLimiter(vm.Disk{ThroughputMiBps: 64})
	if limiter == nil || limiter.Bandwidth == nil {
		t.Fatal("bandwidth limit must be set")
	}
	if limiter.Bandwidth.Size != 64*1024*1024 {
		t.Fatalf("bandwidth = %+v, want 64 MiB", limiter.Bandwidth)
	}
	if limiter.Ops != nil {
		t.Fatalf("ops = %+v, want none", limiter.Ops)
	}
}

// A zero limit is unlimited, so Firecracker must receive no bucket for it.
func TestDriveRateLimiterSkipsUnlimitedValues(t *testing.T) {
	if limiter := driveRateLimiter(vm.Disk{}); limiter != nil {
		t.Fatalf("limiter = %+v, want none", limiter)
	}
	if limiter := driveRateLimiter(vm.Disk{IOPS: 500}); limiter.Bandwidth != nil {
		t.Fatalf("bandwidth = %+v, want none", limiter.Bandwidth)
	}
}

// Firecracker's default cache type drops guest flushes, which loses the last
// writes of a guest that syncs before its disk is photographed.
func TestDriveRequestIsWriteBackCached(t *testing.T) {
	request := driveRequest(0, storage.Drive{Path: "/rootfs.img", Root: true}, vm.Disk{})

	if request.CacheType != api.CacheTypeWriteback {
		t.Fatalf("cache type = %q, want %q", request.CacheType, api.CacheTypeWriteback)
	}
	if request.DriveID != "drive0" || request.PathOnHost != "/rootfs.img" || !request.IsRootDevice {
		t.Fatalf("drive = %+v", request)
	}
}

func TestDriveRequestDiscardsOnlyOnWritableDrives(t *testing.T) {
	if !driveRequest(0, storage.Drive{Path: "/rootfs.img", Root: true}, vm.Disk{}).Discard {
		t.Fatal("writable drive has no discard")
	}
	if driveRequest(1, storage.Drive{Path: "/seed.img", ReadOnly: true}, vm.Disk{}).Discard {
		t.Fatal("read-only drive has discard")
	}
}
