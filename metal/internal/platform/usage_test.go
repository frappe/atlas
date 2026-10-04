package platform

import "testing"

func TestParseDiskUsageSelectsRootDevice(t *testing.T) {
	data := []byte("8:0 rbytes=4096 wbytes=4096 rios=1 wios=1\n230:96 rbytes=16777216 wbytes=8192 rios=20 wios=2 dbytes=0\n")
	usage, err := parseDiskUsage(data, "230:96")
	if err != nil {
		t.Fatal(err)
	}
	if usage.DiskReadBytes != 16777216 || usage.DiskWriteBytes != 8192 ||
		usage.DiskReadOperations != 20 || usage.DiskWriteOperations != 2 {
		t.Fatalf("root disk counters: %+v", usage)
	}
}
