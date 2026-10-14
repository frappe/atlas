package system

import (
	"context"
	"errors"
	"testing"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// Trimmed from smartctl 7.5 on a Samsung SATA SSD, with a reallocated sector
// count past its threshold.
const failingSATAReport = `{"smartctl":{"exit_status":24},"device":{"protocol":"ATA"},
"model_name":"SAMSUNG MZ7LN1T0HMJP-00000","serial_number":"S2MP","firmware_version":"MAV0100Q",
"smart_status":{"passed":false},"temperature":{"current":23},"power_on_time":{"hours":47405},"power_cycle_count":26,
"ata_smart_attributes":{"table":[{"id":5,"name":"Reallocated_Sector_Ct","value":9,"worst":9,"thresh":10,"raw":{"value":2310}}]},
"ata_smart_error_log":{"summary":{"count":3}}}`

func TestSMARTReportsAFailingSATADisk(t *testing.T) {
	sysRoot := writeFiles(t, map[string]string{"block/sda/device/state": "running\n"})
	run := func(_ context.Context, _ string, args ...string) (string, error) {
		if args[len(args)-1] != "/dev/sda" {
			t.Fatalf("smartctl ran on %s", args[len(args)-1])
		}
		return failingSATAReport, errors.New("exit status 24")
	}
	collector := &smartCollector{sysRoot: sysRoot, run: run}

	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	disk := telemetry.Labels{"device": "sda"}
	if healthy := valueOf(t, samples, "system_smart_healthy", disk); healthy != 0 {
		t.Fatalf("healthy = %v, want 0", healthy)
	}
	if status := valueOf(t, samples, "system_smart_exit_status", disk); status != 24 {
		t.Fatalf("exit status = %v, want 24", status)
	}
	reallocated := telemetry.Labels{"device": "sda", "id": "5", "attribute": "Reallocated_Sector_Ct", "field": "raw"}
	if raw := valueOf(t, samples, "system_smart_attribute", reallocated); raw != 2310 {
		t.Fatalf("reallocated sectors = %v, want 2310", raw)
	}

	if again, _ := collector.Collect(context.Background()); again != nil {
		t.Fatal("smartctl ran again before the interval")
	}
}
