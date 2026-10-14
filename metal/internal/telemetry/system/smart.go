package system

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"strconv"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// Limit how often SMART commands reach the disk.
const smartInterval = 10 * time.Minute

type smartReport struct {
	Smartctl struct {
		ExitStatus int `json:"exit_status"`
	} `json:"smartctl"`
	Device struct {
		Protocol string `json:"protocol"`
	} `json:"device"`
	ModelName       string `json:"model_name"`
	SerialNumber    string `json:"serial_number"`
	FirmwareVersion string `json:"firmware_version"`
	SmartStatus     *struct {
		Passed bool `json:"passed"`
	} `json:"smart_status"`
	Temperature struct {
		Current *float64 `json:"current"`
	} `json:"temperature"`
	PowerOnTime struct {
		Hours *float64 `json:"hours"`
	} `json:"power_on_time"`
	PowerCycleCount    *float64 `json:"power_cycle_count"`
	ATASmartAttributes struct {
		Table []struct {
			ID        int     `json:"id"`
			Name      string  `json:"name"`
			Value     float64 `json:"value"`
			Worst     float64 `json:"worst"`
			Threshold float64 `json:"thresh"`
			Raw       struct {
				Value float64 `json:"value"`
			} `json:"raw"`
		} `json:"table"`
	} `json:"ata_smart_attributes"`
	ATASmartErrorLog struct {
		Summary struct {
			Count *float64 `json:"count"`
		} `json:"summary"`
	} `json:"ata_smart_error_log"`
	SCSIGrownDefectList *float64 `json:"scsi_grown_defect_list"`
	SCSIErrorCounterLog map[string]struct {
		TotalUncorrectedErrors float64 `json:"total_uncorrected_errors"`
	} `json:"scsi_error_counter_log"`
}

type smartCollector struct {
	sysRoot string
	// run returns smartctl output even when it exits with a health bit set.
	run  func(ctx context.Context, name string, args ...string) (string, error)
	next time.Time
}

// Collect reports SATA and SAS disk health at smartInterval.
func (collector *smartCollector) Collect(ctx context.Context) ([]telemetry.Sample, error) {
	if time.Now().Before(collector.next) {
		return nil, nil
	}
	collector.next = time.Now().Add(smartInterval)

	disks, _ := filepath.Glob(filepath.Join(collector.sysRoot, "block", "sd*", "device"))

	var samples []telemetry.Sample
	var failures []error
	for _, disk := range disks {
		device := filepath.Base(filepath.Dir(disk))
		// --nocheck=standby leaves a spun-down disk asleep.
		output, runErr := collector.run(ctx, "smartctl", "--json=c", "--nocheck=standby", "-a", "/dev/"+device)
		var report smartReport
		if err := json.Unmarshal([]byte(output), &report); err != nil {
			failures = append(failures, fmt.Errorf("smartctl %s: %w", device, errors.Join(runErr, err)))
			continue
		}
		samples = append(samples, report.samples(device)...)
	}
	return samples, errors.Join(failures...)
}

// samples converts one report. exit_status is the smartctl bitmask: bit 3 is a
// failing disk, bit 4 a prefailure attribute at its threshold, bit 6 errors in
// the error log, and bit 7 a failed self-test.
func (report smartReport) samples(device string) []telemetry.Sample {
	labels := telemetry.Labels{"device": device}
	samples := []telemetry.Sample{
		gauge("system_smart_info", 1, telemetry.Labels{
			"device": device, "protocol": report.Device.Protocol, "model": report.ModelName,
			"serial": report.SerialNumber, "firmware": report.FirmwareVersion,
		}),
		gauge("system_smart_exit_status", float64(report.Smartctl.ExitStatus), labels),
	}
	optional := func(metric string, value *float64, scale float64) {
		if value != nil {
			samples = append(samples, gauge(metric, *value*scale, labels))
		}
	}

	if report.SmartStatus != nil {
		healthy := 0.0
		if report.SmartStatus.Passed {
			healthy = 1
		}
		samples = append(samples, gauge("system_smart_healthy", healthy, labels))
	}
	optional("system_smart_temperature_celsius", report.Temperature.Current, 1)
	optional("system_smart_power_on_seconds", report.PowerOnTime.Hours, 3600)
	optional("system_smart_power_cycles", report.PowerCycleCount, 1)
	optional("system_smart_error_log_entries", report.ATASmartErrorLog.Summary.Count, 1)
	optional("system_smart_scsi_grown_defects", report.SCSIGrownDefectList, 1)

	for operation, counters := range report.SCSIErrorCounterLog {
		samples = append(samples, gauge("system_smart_scsi_uncorrected_errors", counters.TotalUncorrectedErrors, telemetry.Labels{"device": device, "operation": operation}))
	}
	for _, attribute := range report.ATASmartAttributes.Table {
		for field, value := range map[string]float64{"value": attribute.Value, "worst": attribute.Worst, "threshold": attribute.Threshold, "raw": attribute.Raw.Value} {
			samples = append(samples, gauge("system_smart_attribute", value, telemetry.Labels{
				"device": device, "id": strconv.Itoa(attribute.ID), "attribute": attribute.Name, "field": field,
			}))
		}
	}
	return samples
}
