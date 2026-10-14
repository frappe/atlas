package system

import (
	"context"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// sensorFile matches hwmon attribute files such as temp1_input and fan2_max.
var sensorFile = regexp.MustCompile(`^(temp|fan|in|curr|power|energy|humidity|pwm|freq|intrusion)(\d+)_(\w+)$`)

// sensorKinds gives the metric and the scale from the sysfs unit to the base unit.
var sensorKinds = map[string]struct {
	metric string
	scale  float64
}{
	"temp":      {"system_hwmon_temperature_celsius", 0.001},
	"fan":       {"system_hwmon_fan_rpm", 1},
	"in":        {"system_hwmon_voltage_volts", 0.001},
	"curr":      {"system_hwmon_current_amperes", 0.001},
	"power":     {"system_hwmon_power_watts", 0.000001},
	"energy":    {"system_hwmon_energy_joules", 0.000001},
	"humidity":  {"system_hwmon_humidity_ratio", 0.00001},
	"pwm":       {"system_hwmon_pwm", 1},
	"freq":      {"system_hwmon_frequency_hertz", 1},
	"intrusion": {"system_hwmon_intrusion", 1},
}

type hardwareCollector struct {
	sysRoot string
}

// Collect reads sensors, thermal and cooling devices, power supplies, RAPL
// energy, memory controller errors, and watchdogs.
func (collector hardwareCollector) Collect(context.Context) ([]telemetry.Sample, error) {
	samples := collector.sensors()
	samples = append(samples, collector.thermal()...)
	samples = append(samples, collector.powerSupplies()...)
	samples = append(samples, collector.rapl()...)
	samples = append(samples, collector.memoryErrors()...)
	return append(samples, collector.watchdogs()...), nil
}

// sensors reads every numeric hwmon attribute. Alarm, fault, and enable
// attributes are flags, so they are not scaled.
func (collector hardwareCollector) sensors() []telemetry.Sample {
	chips, _ := filepath.Glob(filepath.Join(collector.sysRoot, "class", "hwmon", "hwmon*"))

	var samples []telemetry.Sample
	for _, chip := range chips {
		chipName := readText(filepath.Join(chip, "name"))
		device := readLinkName(filepath.Join(chip, "device"))
		entries, _ := os.ReadDir(chip)
		for _, entry := range entries {
			match := sensorFile.FindStringSubmatch(entry.Name())
			if match == nil {
				continue
			}
			value, err := readNumber(filepath.Join(chip, entry.Name()))
			if err != nil {
				continue
			}
			kind, attribute := sensorKinds[match[1]], match[3]
			if !strings.HasSuffix(attribute, "alarm") && !strings.HasSuffix(attribute, "fault") &&
				!strings.HasSuffix(attribute, "enable") && !strings.HasSuffix(attribute, "beep") {
				value *= kind.scale
			}
			sensor := readText(filepath.Join(chip, match[1]+match[2]+"_label"))
			if sensor == "" {
				sensor = match[1] + match[2]
			}
			labels := telemetry.Labels{"chip": chipName, "device": device, "sensor": sensor, "attribute": attribute}
			samples = append(samples, gauge(kind.metric, value, labels))
		}
	}
	return samples
}

// thermal reads thermal zone temperatures and cooling device throttle states.
func (collector hardwareCollector) thermal() []telemetry.Sample {
	var samples []telemetry.Sample
	zones, _ := filepath.Glob(filepath.Join(collector.sysRoot, "class", "thermal", "thermal_zone*"))
	for _, zone := range zones {
		if value, err := readNumber(filepath.Join(zone, "temp")); err == nil {
			labels := telemetry.Labels{"zone": filepath.Base(zone), "type": readText(filepath.Join(zone, "type"))}
			samples = append(samples, gauge("system_thermal_zone_temperature_celsius", value/1000, labels))
		}
	}

	devices, _ := filepath.Glob(filepath.Join(collector.sysRoot, "class", "thermal", "cooling_device*"))
	for _, device := range devices {
		labels := telemetry.Labels{"device": filepath.Base(device), "type": readText(filepath.Join(device, "type"))}
		if value, err := readNumber(filepath.Join(device, "cur_state")); err == nil {
			samples = append(samples, gauge("system_cooling_device_current_state", value, labels))
		}
		if value, err := readNumber(filepath.Join(device, "max_state")); err == nil {
			samples = append(samples, gauge("system_cooling_device_max_state", value, labels))
		}
	}
	return samples
}

// powerSupplies reports each numeric attribute of each power supply.
func (collector hardwareCollector) powerSupplies() []telemetry.Sample {
	supplies, _ := filepath.Glob(filepath.Join(collector.sysRoot, "class", "power_supply", "*"))

	var samples []telemetry.Sample
	for _, supply := range supplies {
		labels := telemetry.Labels{"supply": filepath.Base(supply), "type": readText(filepath.Join(supply, "type"))}
		samples = append(samples, numberFiles(supply, "system_power_supply", labels)...)
	}
	return samples
}

// rapl reads the energy counters of each RAPL zone, such as a CPU package.
func (collector hardwareCollector) rapl() []telemetry.Sample {
	zones, _ := filepath.Glob(filepath.Join(collector.sysRoot, "class", "powercap", "intel-rapl:*"))

	var samples []telemetry.Sample
	for _, zone := range zones {
		microjoules, err := readNumber(filepath.Join(zone, "energy_uj"))
		if err != nil {
			continue
		}
		labels := telemetry.Labels{"zone": filepath.Base(zone), "name": readText(filepath.Join(zone, "name"))}
		samples = append(samples, gauge("system_rapl_energy_joules_total", microjoules/1e6, labels))
	}
	return samples
}

// memoryErrors reads EDAC counters per controller and per chip-select row. A
// rising corrected count warns of a failing DIMM.
func (collector hardwareCollector) memoryErrors() []telemetry.Sample {
	controllers, _ := filepath.Glob(filepath.Join(collector.sysRoot, "devices", "system", "edac", "mc", "mc*"))

	var samples []telemetry.Sample
	for _, controller := range controllers {
		labels := telemetry.Labels{"controller": filepath.Base(controller)}
		samples = append(samples, edacCounts(controller, labels)...)

		rows, _ := filepath.Glob(filepath.Join(controller, "csrow*"))
		for _, row := range rows {
			samples = append(samples, edacCounts(row, telemetry.Labels{"controller": filepath.Base(controller), "csrow": filepath.Base(row)})...)
		}
	}
	return samples
}

func edacCounts(directory string, labels telemetry.Labels) []telemetry.Sample {
	var samples []telemetry.Sample
	if value, err := readNumber(filepath.Join(directory, "ce_count")); err == nil {
		samples = append(samples, gauge("system_edac_correctable_errors_total", value, labels))
	}
	if value, err := readNumber(filepath.Join(directory, "ue_count")); err == nil {
		samples = append(samples, gauge("system_edac_uncorrectable_errors_total", value, labels))
	}
	return samples
}

// watchdogs reports each numeric attribute of each hardware watchdog.
func (collector hardwareCollector) watchdogs() []telemetry.Sample {
	devices, _ := filepath.Glob(filepath.Join(collector.sysRoot, "class", "watchdog", "watchdog*"))

	var samples []telemetry.Sample
	for _, device := range devices {
		labels := telemetry.Labels{"device": filepath.Base(device), "identity": readText(filepath.Join(device, "identity"))}
		samples = append(samples, numberFiles(device, "system_watchdog", labels)...)
	}
	return samples
}
