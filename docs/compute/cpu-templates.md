# Guest CPU templates

Metal selects a versioned guest CPU template at daemon startup. It embeds the templates in the binary, so host installation does not need separate JSON files. Each cold boot applies the selected template through Firecracker's `/cpu-config` API before the guest starts.

## Profiles

| Host | Built-in template | Guest instruction baseline |
| --- | --- | --- |
| Intel x86-64 | `intel-x86_64-v1.json` | x86-64-v2 with basic XSAVE. |
| AMD x86-64 | `amd-x86_64-v1.json` | x86-64-v2 with basic XSAVE. |
| ARM64 | `arm64-v1.json` | ARMv8-A floating point and Advanced SIMD. |

The JSON files are in [`metal/internal/firecracker/cpu_templates`](../../metal/internal/firecracker/cpu_templates). The profiles mask optional instruction extensions instead of enabling features that a host might not support. Host identity, topology, address limits, and security information remain host-dependent. Firecracker can normalize some CPU fields after it applies the template.

The x86-64 profiles retain SSE3, SSSE3, CMPXCHG16B, SSE4.1, SSE4.2, POPCNT, LAHF/SAHF, and XSAVE. They hide AES, AVX, AVX2, AVX-512, FMA, F16C, BMI1, BMI2, INVPCID, and other optional instruction extensions. Extended save state is limited to x87 and SSE. The AMD profile retains AMD topology enumeration. Startup rejects an unknown x86 vendor or a host without the baseline features.

The ARM64 profile hides optional scalar instruction extensions, FP16, SVE, pointer authentication, BTI, MTE, SME, and performance monitoring. It retains the execution levels and PSCI boot configuration. ARM hosts need a kernel that permits the profile's CPU ID register writes. Platform registers and interrupt-controller state still require compatible hosts.

### Register masks

| Architecture | Field | Purpose |
| --- | --- | --- |
| x86-64 | CPUID `0x1` ECX and EDX | Keep the baseline and basic XSAVE; remove optional instruction and machine-check features. |
| x86-64 | CPUID `0x7`, subleaf `0` | Hide newer instruction extensions and subleaves. Keep host mitigation signals in EDX. |
| x86-64 | CPUID `0xd`, subleaves `0` and `1` | Keep x87 and SSE state; disable newer XSAVE variants. |
| x86-64 | CPUID `0x80000001` | Hide vendor extensions, RDTSCP, and 1 GiB pages. Keep LAHF/SAHF and AMD topology enumeration. |
| x86-64 | CPUID `0x80000008` EBX | Hide CLZERO and WBNOINVD. |
| ARM64 | vCPU initialization bits `3` through `6` | Disable PMU, SVE, and pointer authentication. |
| ARM64 | `ID_AA64PFR0_EL1` and `ID_AA64PFR1_EL1` | Hide FP16, RAS, SVE, AMU, DIT, RME, BTI, MTE, and SME. |
| ARM64 | `ID_AA64ISAR0_EL1`, `ID_AA64ISAR1_EL1`, and `ID_AA64ISAR2_EL1` | Hide optional scalar instructions, including cryptography and LSE atomics. |
| ARM64 | `ID_AA64MMFR2_EL1.AT` | Hide enhanced atomicity. |

Lower feature exposure can reduce application performance. Guest binaries must support the profile's baseline. Templates are a feature declaration, not instruction emulation: they cannot prevent a program from executing a hidden instruction on hardware that implements it.

## Override

Set `firecracker.cpu_template_file` in `metald.toml` to use an explicit JSON template. A missing or empty setting selects the built-in host profile. Metal loads an override once at startup. An unreadable file or invalid JSON stops daemon startup. Firecracker validates its schema before guest boot.

Changing a template requires a daemon restart and a cold boot of each affected guest. Existing running guests retain their CPU configuration. Snapshot restores use the configuration captured in guest state. Shared warm images and VM-local snapshots include the selected template digest in their compatibility marker; regenerate snapshots after changing a profile.

## Migration validation

A common instruction baseline reduces differences between hosts. It does not guarantee memory migration between arbitrary CPU models. Intel-to-AMD and cross-architecture snapshot restores are unsupported. x86 hosts need compatible TSC frequencies or TSC scaling. ARM hosts need compatible CPU and interrupt-controller state. Use disk-only migration within one architecture when memory migration is unsupported.

For each supported CPU, kernel, and Firecracker combination, use the [Firecracker CPU template helper](https://github.com/firecracker-microvm/firecracker/blob/v1.16.1/docs/cpu_templates/cpu-template-helper.md) to dump and verify the guest configuration. Verification checks that the template is applied; it does not prove that a guest will boot or migrate correctly. Test guest boot and memory migration in both directions before adding hosts to a migration group.

```sh
cpu-template-helper template verify \
  --template intel-x86_64-v1.json
```

The built-in profiles have unit checks for selection, baseline preservation, and optional feature masks. Both x86 JSON profiles passed Firecracker v1.16.1's helper verification, a real guest CPUID check, and snapshot resume on Intel hardware. AMD and ARM hardware validation is required before deployment on those hosts.

See [VM runtime](runtime.md#common-cpu-configuration), [migration engine](migration-engine.md#copy-the-disk), and [Firecracker snapshot compatibility](https://github.com/firecracker-microvm/firecracker/blob/v1.16.1/docs/snapshotting/versioning.md#cpu-model).
