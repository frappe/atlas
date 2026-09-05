package firecracker

import (
	"testing"

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

// A zero limit is unlimited, so Firecracker must receive no bucket for it.
func TestDriveRateLimiterSkipsUnlimitedValues(t *testing.T) {
	if limiter := driveRateLimiter(vm.Disk{}); limiter != nil {
		t.Fatalf("limiter = %+v, want none", limiter)
	}
	if limiter := driveRateLimiter(vm.Disk{IOPS: 500}); limiter.Bandwidth != nil {
		t.Fatalf("bandwidth = %+v, want none", limiter.Bandwidth)
	}
}
