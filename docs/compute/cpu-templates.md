# Guest CPU templates

Metal selects a versioned guest CPU template at daemon startup. It embeds the templates in the binary, so host installation does not need separate JSON files. Each cold boot applies the selected template through Firecracker's `/cpu-config` API before the guest starts.

## Profiles

| Host | Built-in template | Guest instruction baseline |
| --- | --- | --- |
| Intel x86-64 | `intel-x86_64-v3.json` | x86-64-v3 with AES and PCLMULQDQ. |
| AMD x86-64 | `amd-x86_64-v3.json` | x86-64-v3 with AES and PCLMULQDQ. |
| ARM64 | `arm64-v1.json` | ARMv8-A floating point and Advanced SIMD. |

The JSON files are in [`metal/internal/firecracker/cpu_templates`](../../metal/internal/firecracker/cpu_templates/). The profiles mask optional instruction extensions instead of enabling features that a host might not support. Host identity, topology, address limits, and security information remain host-dependent. Firecracker can normalize some CPU fields after it applies the template.

The x86-64 profiles retain the x86-64-v2 instructions (SSE3, SSSE3, CMPXCHG16B, SSE4.1, SSE4.2, POPCNT, and LAHF/SAHF) and XSAVE. They add the x86-64-v3 instructions (AVX, AVX2, BMI1, BMI2, F16C, FMA, LZCNT, and MOVBE), AES, and PCLMULQDQ.

Common x86-64 builds, such as ClickHouse, require PCLMULQDQ and stop with an illegal instruction without it.

The x86-64 profiles hide AVX-512, RDRAND, INVPCID, and other optional instruction extensions. Extended save state is limited to x87, SSE, and AVX. The AMD profile retains AMD topology enumeration.

A host with an unknown x86 vendor or without the baseline features boots its VMs without a CPU template. metald logs a warning and reports the reason in `default_cpu_template_error`. Atlas then sets **Disable CPU Template** on the Metal Server and refuses to clear it. Intel CPUs before Haswell and AMD CPUs before Excavator do not have AVX2, so they cannot use the built-in profiles.

The ARM64 profile hides optional scalar instruction extensions, FP16, SVE, pointer authentication, BTI, MTE, SME, and performance monitoring. It retains the execution levels and PSCI boot configuration. ARM hosts need a kernel that permits the profile's CPU ID register writes. Platform registers and interrupt-controller state still require compatible hosts.

### Register masks

| Architecture | Field | Purpose |
| --- | --- | --- |
| x86-64 | CPUID `0x1` ECX and EDX | Keep the baseline, AES, PCLMULQDQ, and XSAVE; remove optional instruction and machine-check features. |
| x86-64 | CPUID `0x7`, subleaf `0` | Keep BMI1, AVX2, and BMI2. Hide newer instruction extensions and subleaves. Keep host mitigation signals in EDX. |
| x86-64 | CPUID `0xd`, subleaves `0` and `1` | Keep x87, SSE, and AVX state; disable newer XSAVE variants. |
| x86-64 | CPUID `0x80000001` | Hide vendor extensions, RDTSCP, and 1 GiB pages. Keep LAHF/SAHF, LZCNT, and AMD topology enumeration. |
| x86-64 | CPUID `0x80000008` EBX | Hide CLZERO and WBNOINVD. |
| ARM64 | vCPU initialization bits `3` through `6` | Disable PMU, SVE, and pointer authentication. |
| ARM64 | `ID_AA64PFR0_EL1` and `ID_AA64PFR1_EL1` | Hide FP16, RAS, SVE, AMU, DIT, RME, BTI, MTE, and SME. |
| ARM64 | `ID_AA64ISAR0_EL1`, `ID_AA64ISAR1_EL1`, and `ID_AA64ISAR2_EL1` | Hide optional scalar instructions, including cryptography and LSE atomics. |
| ARM64 | `ID_AA64MMFR2_EL1.AT` | Hide enhanced atomicity. |

Lower feature exposure can reduce application performance. Guest binaries must support the profile's baseline. Templates are a feature declaration, not instruction emulation: they cannot prevent a program from executing a hidden instruction on hardware that implements it.

## Override

Set `firecracker.cpu_template_file` in `metald.toml` to use an explicit JSON template. A missing or empty setting selects the built-in host profile. Metal loads an override once at startup. An unreadable file or invalid JSON stops daemon startup. Firecracker validates its schema before guest boot.

Select **Disable CPU Template** on a Metal Server and run **Upgrade Metald** to boot every VM on that host without a CPU template. Atlas then writes `cpu_template_disabled = true` into the `[firecracker]` section of `metald.toml` and restarts metald. Atlas owns this line: install and upgrade remove it when the check is clear.

Without a CPU template, the guests see the host CPU features that Firecracker exposes by default. Metal reports the CPU profile `native`, so a memory snapshot migration needs a destination with the same CPU model and features. Startup rejects this setting together with `cpu_template_file`.

Changing a template requires a daemon restart and a cold boot of each affected guest. Existing running guests retain their CPU configuration. Snapshot restores use the configuration captured in guest state. Shared warm images and VM-local snapshots include the selected template digest in their compatibility marker; regenerate snapshots after changing a profile.

## Migration validation

A common instruction baseline reduces differences between hosts. It does not guarantee memory snapshot migration between arbitrary CPU models. Intel-to-AMD and cross-architecture snapshot restores are unsupported. x86 hosts need compatible TSC frequencies or TSC scaling. ARM hosts need compatible CPU and interrupt-controller state. Use disk-only migration within one architecture when memory snapshot migration is unsupported.

For each supported CPU, kernel, and Firecracker combination, use the [Firecracker CPU template helper](https://github.com/firecracker-microvm/firecracker/blob/v1.16.1/docs/cpu_templates/cpu-template-helper.md) to dump and verify the guest configuration. Verification checks that the template is applied; it does not prove that a guest will boot or migrate correctly. Test guest boot and memory snapshot migration in both directions before adding hosts to a migration group.

```sh
cpu-template-helper template verify \
  --template intel-x86_64-v3.json
```

The built-in profiles have unit checks for selection, baseline preservation, and optional feature masks. The Intel x86 profile passed a real guest CPUID check on Intel hardware with Firecracker v1.10.1. Helper verification, snapshot resume, and AMD and ARM hardware validation are required before deployment.

See [VM runtime](runtime.md#common-cpu-configuration), [migration engine](migration-engine.md#copy-the-disk), and [Firecracker snapshot compatibility](https://github.com/firecracker-microvm/firecracker/blob/v1.16.1/docs/snapshotting/versioning.md#cpu-model).
