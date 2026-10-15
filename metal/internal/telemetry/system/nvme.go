package system

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"math"
	"math/big"
	"os"
	"path/filepath"
	"runtime"
	"unsafe"

	"golang.org/x/sys/unix"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// NVMe admin passthrough: _IOWR('N', 0x41, struct nvme_passthru_cmd), 72 bytes.
const (
	nvmeAdminCommand  = 0xC0484E41
	nvmeGetLogPage    = 0x02
	nvmeSMARTLog      = 0x02
	nvmeSMARTLogBytes = 512
)

// nvmePassthroughCommand is the kernel struct nvme_passthru_cmd.
type nvmePassthroughCommand struct {
	opcode         uint8
	flags          uint8
	reserved       uint16
	namespaceID    uint32
	commandDword2  uint32
	commandDword3  uint32
	metadata       uint64
	address        uint64
	metadataLength uint32
	dataLength     uint32
	commandDword10 uint32
	commandDword11 uint32
	commandDword12 uint32
	commandDword13 uint32
	commandDword14 uint32
	commandDword15 uint32
	timeoutMillis  uint32
	result         uint32
}

// smartFields are the SMART health log values after the warning, temperature,
// and spare bytes. Each is a 128-bit little-endian count at offset.
var smartFields = []struct {
	offset int
	field  string
	scale  float64
}{
	{32, "data_read_bytes", 512000}, {48, "data_written_bytes", 512000},
	{64, "host_read_commands", 1}, {80, "host_write_commands", 1},
	{96, "controller_busy_seconds", 60}, {112, "power_cycles", 1},
	{128, "power_on_seconds", 3600}, {144, "unsafe_shutdowns", 1},
	{160, "media_errors", 1}, {176, "error_log_entries", 1},
}

type nvmeCollector struct {
	sysRoot string
	devRoot string
}

// Collect reports each NVMe controller's identity and SMART health log: wear,
// spare capacity, media errors, and the critical warning bits.
func (collector nvmeCollector) Collect(context.Context) ([]telemetry.Sample, error) {
	controllers, _ := filepath.Glob(filepath.Join(collector.sysRoot, "class", "nvme", "nvme*"))

	var samples []telemetry.Sample
	var failures []error
	for _, controller := range controllers {
		device := filepath.Base(controller)
		samples = append(samples, gauge("system_nvme_info", 1, telemetry.Labels{
			"device": device, "model": readText(filepath.Join(controller, "model")),
			"serial":   readText(filepath.Join(controller, "serial")),
			"firmware": readText(filepath.Join(controller, "firmware_rev")),
			"state":    readText(filepath.Join(controller, "state")),
		}))

		log, err := readSMARTLog(filepath.Join(collector.devRoot, device))
		if err != nil {
			failures = append(failures, fmt.Errorf("read SMART log of %s: %w", device, err))
			continue
		}
		samples = append(samples, parseSMARTLog(device, log)...)
	}
	return samples, errors.Join(failures...)
}

// readSMARTLog asks the controller for its SMART health log page.
func readSMARTLog(path string) ([]byte, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	log := make([]byte, nvmeSMARTLogBytes)
	command := nvmePassthroughCommand{
		opcode:         nvmeGetLogPage,
		namespaceID:    math.MaxUint32,
		address:        uint64(uintptr(unsafe.Pointer(&log[0]))),
		dataLength:     nvmeSMARTLogBytes,
		commandDword10: (nvmeSMARTLogBytes/4-1)<<16 | nvmeSMARTLog,
	}
	_, _, errno := unix.Syscall(unix.SYS_IOCTL, file.Fd(), nvmeAdminCommand, uintptr(unsafe.Pointer(&command)))
	// The kernel writes into log through a raw address the collector cannot see.
	runtime.KeepAlive(log)
	if errno != 0 {
		return nil, errno
	}
	return log, nil
}

// parseSMARTLog reads the fields of an NVMe SMART health log page.
func parseSMARTLog(device string, log []byte) []telemetry.Sample {
	if len(log) < nvmeSMARTLogBytes {
		return nil
	}
	labels := func(field string) telemetry.Labels { return telemetry.Labels{"device": device, "field": field} }
	samples := []telemetry.Sample{
		gauge("system_nvme_smart", float64(log[0]), labels("critical_warning")),
		gauge("system_nvme_smart", float64(binary.LittleEndian.Uint16(log[1:3]))-273.15, labels("temperature_celsius")),
		gauge("system_nvme_smart", float64(log[3]), labels("available_spare_percent")),
		gauge("system_nvme_smart", float64(log[4]), labels("available_spare_threshold_percent")),
		gauge("system_nvme_smart", float64(log[5]), labels("percentage_used")),
	}
	for _, field := range smartFields {
		value := littleEndian128(log[field.offset : field.offset+16])
		samples = append(samples, gauge("system_nvme_smart", value*field.scale, labels(field.field)))
	}
	return samples
}

// littleEndian128 converts a 128-bit little-endian count to a float.
func littleEndian128(bytes []byte) float64 {
	bigEndian := make([]byte, len(bytes))
	for index, value := range bytes {
		bigEndian[len(bytes)-1-index] = value
	}
	value, _ := new(big.Float).SetInt(new(big.Int).SetBytes(bigEndian)).Float64()
	return value
}
