package firecracker

import (
	"strconv"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

// rateLimiterRefillMilliseconds sizes the token bucket over one second.
const rateLimiterRefillMilliseconds = 1000

// driveRateLimiter converts disk limits to Firecracker token buckets.
func driveRateLimiter(disk vm.Disk, driveCount int) *api.RateLimiter {
	limiter := api.RateLimiter{}
	if disk.ThroughputMiBps > 0 {
		limiter.Bandwidth = &api.TokenBucket{
			Size: int64(disk.ThroughputMiBps) * 1024 * 1024 / int64(driveCount), RefillTime: rateLimiterRefillMilliseconds,
		}
	}
	if disk.IOPS > 0 {
		limiter.Ops = &api.TokenBucket{Size: int64(disk.IOPS), RefillTime: rateLimiterRefillMilliseconds * int64(driveCount)}
		if disk.IOPS%driveCount == 0 {
			limiter.Ops.Size /= int64(driveCount)
			limiter.Ops.RefillTime = rateLimiterRefillMilliseconds
		}
	}
	if limiter.Bandwidth == nil && limiter.Ops == nil {
		return nil
	}
	return &limiter
}

// driveRequest builds the Firecracker drive for one boot drive. Every drive is
// write-back cached so that a guest flush reaches the host file, which a disk
// snapshot taken from outside the guest then sees.
func driveRequest(index int, drive storage.Drive, disk vm.Disk, driveCount int) api.Drive {
	return api.Drive{
		DriveID:      "drive" + strconv.Itoa(index),
		PathOnHost:   drive.Path,
		IsRootDevice: drive.Root,
		IsReadOnly:   drive.ReadOnly,
		CacheType:    api.CacheTypeWriteback,
		RateLimiter:  driveRateLimiter(disk, driveCount),
	}
}
