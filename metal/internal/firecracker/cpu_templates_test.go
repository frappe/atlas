package firecracker

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"testing"
)

func TestDefaultCPUTemplateSelection(t *testing.T) {
	x86 := "vendor_id: %s\ncpu family: 6\nmodel: 58\nflags: pni ssse3 cx16 sse4_1 sse4_2 popcnt lahf_lm xsave avx avx2 bmi1 bmi2 f16c fma abm movbe aes pclmulqdq avx512f\n"
	arm := "CPU implementer: 0x41\nCPU architecture: 8\nCPU part: 0xd0c\nCPU variant: 0x1\nCPU revision: 2\nFeatures: fp asimd aes sve\n"
	for _, sample := range []struct {
		architecture, information, name string
	}{
		{"amd64", fmt.Sprintf(x86, "GenuineIntel"), "intel-x86_64-v3"},
		{"amd64", fmt.Sprintf(x86, "AuthenticAMD"), "amd-x86_64-v3"},
		{"arm64", arm, "arm64-v1"},
	} {
		t.Run(sample.name, func(t *testing.T) {
			template, err := cpuTemplateForHost(sample.architecture, sample.information)
			if err != nil {
				t.Fatal(err)
			}
			want, err := cpuTemplates.ReadFile("cpu_templates/" + sample.name + ".json")
			if err != nil || !bytes.Equal(template, want) {
				t.Fatalf("selected wrong CPU template: %v", err)
			}
		})
	}
	for _, sample := range []struct{ architecture, information string }{
		{"amd64", fmt.Sprintf(x86, "UnknownVendor")},
		{"amd64", strings.ReplaceAll(fmt.Sprintf(x86, "GenuineIntel"), "sse4_2", "")},
		{"amd64", strings.ReplaceAll(fmt.Sprintf(x86, "AuthenticAMD"), "pclmulqdq", "")},
		{"amd64", strings.ReplaceAll(fmt.Sprintf(x86, "AuthenticAMD"), "avx2", "")},
		{"arm64", strings.ReplaceAll(arm, "asimd", "")},
		{"amd64", "processor: 0"},
		{"riscv64", arm},
	} {
		if _, err := cpuTemplateForHost(sample.architecture, sample.information); err == nil {
			t.Fatalf("accepted unsupported CPU: %s %s", sample.architecture, sample.information)
		}
	}
}

func TestX86TemplatesMaskOptionalInstructionsAndKeepBaseline(t *testing.T) {
	for _, name := range []string{"intel-x86_64-v3", "amd-x86_64-v3"} {
		t.Run(name, func(t *testing.T) {
			data, err := cpuTemplates.ReadFile("cpu_templates/" + name + ".json")
			if err != nil {
				t.Fatal(err)
			}
			var template struct {
				Leaves []struct {
					Leaf      string `json:"leaf"`
					Subleaf   string `json:"subleaf"`
					Modifiers []struct {
						Register string `json:"register"`
						Bitmap   string `json:"bitmap"`
					} `json:"modifiers"`
				} `json:"cpuid_modifiers"`
			}
			if err := json.Unmarshal(data, &template); err != nil {
				t.Fatal(err)
			}
			values := make(map[string]uint64)
			for _, leaf := range template.Leaves {
				for _, modifier := range leaf.Modifiers {
					bitmap := strings.TrimPrefix(modifier.Bitmap, "0b")
					if len(bitmap) != 32 || strings.Contains(bitmap, "1") {
						t.Fatal("template enables an optional hardware feature")
					}
					value, err := strconv.ParseUint(strings.ReplaceAll(bitmap, "x", "1"), 2, 32)
					if err != nil {
						t.Fatal(err)
					}
					values[leaf.Leaf+":"+leaf.Subleaf+":"+modifier.Register] = value
				}
			}
			basic := values["0x1:0x0:ecx"]
			if basic&(1<<30) != 0 {
				t.Fatal("RDRAND remains exposed")
			}
			for _, bit := range []uint{0, 1, 9, 12, 13, 19, 20, 22, 23, 25, 26, 28, 29} {
				if basic&(1<<bit) == 0 {
					t.Fatal("template removed a baseline instruction or XSAVE")
				}
			}
			extended, found := values["0x7:0x0:ebx"]
			if !found || extended&((1<<3)|(1<<5)|(1<<8)) != (1<<3)|(1<<5)|(1<<8) {
				t.Fatal("template removed BMI1, AVX2 or BMI2")
			}
			if extended&((1<<10)|(1<<16)) != 0 {
				t.Fatal("INVPCID or AVX-512 remains exposed")
			}
			if values["0x80000001:0x0:ecx"]&(1<<5) == 0 {
				t.Fatal("template removed LZCNT")
			}
			if values["0xd:0x0:eax"] != 7 || values["0xd:0x0:edx"] != 0 {
				t.Fatal("template exposed extended register state beyond x87, SSE and AVX")
			}
		})
	}
}

func TestARMTemplateKeepsBootFeaturesAndMasksOptionalInstructions(t *testing.T) {
	data, err := cpuTemplates.ReadFile("cpu_templates/arm64-v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var template struct {
		Features  []struct{ Bitmap string } `json:"vcpu_features"`
		Registers []struct {
			Address string `json:"addr"`
			Bitmap  string `json:"bitmap"`
		} `json:"reg_modifiers"`
	}
	if err := json.Unmarshal(data, &template); err != nil {
		t.Fatal(err)
	}
	features, err := strconv.ParseUint(strings.ReplaceAll(strings.TrimPrefix(template.Features[0].Bitmap, "0b"), "x", "1"), 2, 32)
	if err != nil || features&7 != 7 || features&0x78 != 0 {
		t.Fatal("ARM template changed boot features or exposed PMU, SVE or pointer authentication")
	}
	registers := make(map[string]uint64)
	for _, register := range template.Registers {
		bitmap := strings.TrimPrefix(register.Bitmap, "0b")
		if len(bitmap) != 64 || strings.Contains(bitmap, "1") {
			t.Fatal("ARM template enables an optional hardware feature")
		}
		value, err := strconv.ParseUint(strings.ReplaceAll(bitmap, "x", "1"), 2, 64)
		if err != nil {
			t.Fatal(err)
		}
		registers[register.Address] = value
	}
	if registers["0x603000000013c020"]&0xffff != 0xffff || registers["0x603000000013c020"]&0xf00ff0000 != 0 {
		t.Fatal("ARM template changed execution levels or exposed FP16 or SVE")
	}
	protection, found := registers["0x603000000013c021"]
	if !found || protection&0xf000f0f != 0 {
		t.Fatal("ARM template exposed BTI, MTE or SME")
	}
	for _, address := range []string{"0x603000000013c030", "0x603000000013c031", "0x603000000013c032"} {
		instructions, found := registers[address]
		if !found || instructions != 0 {
			t.Fatal("ARM template exposed optional scalar instructions")
		}
	}
}
