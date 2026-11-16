package guestagent

import (
	"path/filepath"
	"slices"
	"testing"
)

func TestFreezableMountPointsSkipsReadOnlyVirtualAndBindMounts(t *testing.T) {
	path := filepath.Join(t.TempDir(), "mounts")
	writeFile(t, path, `/dev/root / ext4 rw,relatime 0 0
proc /proc proc rw,nosuid 0 0
/dev/vdb /data xfs rw,relatime 0 0
/dev/vdb /srv/data xfs rw,relatime 0 0
/dev/vdc /mnt/iso iso9660 ro,relatime 0 0
`)

	mountPoints, err := freezableMountPoints(path)
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"/", "/data"}; !slices.Equal(mountPoints, want) {
		t.Fatalf("mount points = %v, want %v", mountPoints, want)
	}
}

func TestRootDeviceUsesTheLastRootMount(t *testing.T) {
	path := filepath.Join(t.TempDir(), "mountinfo")
	writeFile(t, path, `21 1 254:0 / / rw,relatime shared:1 - ext4 /dev/root rw
22 21 0:5 / /proc rw,nosuid shared:2 - proc proc rw
30 21 254:16 / / rw,relatime - xfs /dev/root rw
`)

	device, filesystemType, err := rootDevice(path)
	if err != nil {
		t.Fatal(err)
	}
	if device != "/dev/block/254:16" || filesystemType != "xfs" {
		t.Fatalf("root device = %s %s", device, filesystemType)
	}
}
