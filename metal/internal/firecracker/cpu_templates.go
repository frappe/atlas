package firecracker

import (
	"embed"
	"encoding/json"
	"fmt"
	"os"
	"runtime"
	"slices"
	"strings"
)

//go:embed cpu_templates/*.json
var cpuTemplates embed.FS

// DefaultCPUTemplate selects the built-in guest CPU baseline for this host.
func DefaultCPUTemplate() (json.RawMessage, error) {
	information, err := os.ReadFile("/proc/cpuinfo")
	if err != nil {
		return nil, fmt.Errorf("read host CPU: %w", err)
	}
	return cpuTemplateForHost(runtime.GOARCH, string(information))
}

func cpuTemplateForHost(architecture, information string) (json.RawMessage, error) {
	cpu, err := parseHostCPU(information, architecture)
	if err != nil {
		return nil, err
	}
	name := "arm64-v1"
	requiredFeatures := []string{"fp", "asimd"}
	if architecture == "amd64" {
		switch cpu.Vendor {
		case "GenuineIntel":
			name = "intel-x86_64-v3"
		case "AuthenticAMD":
			name = "amd-x86_64-v3"
		default:
			return nil, fmt.Errorf("no default CPU template for vendor %q", cpu.Vendor)
		}
		requiredFeatures = []string{
			"pni", "ssse3", "cx16", "sse4_1", "sse4_2", "popcnt", "lahf_lm", "xsave",
			"avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "abm", "movbe", "aes", "pclmulqdq",
		}
	}
	features := strings.Fields(cpu.Features)
	for _, feature := range requiredFeatures {
		if !slices.Contains(features, feature) {
			return nil, fmt.Errorf("default CPU template requires host feature %s", feature)
		}
	}
	return cpuTemplates.ReadFile("cpu_templates/" + name + ".json")
}
