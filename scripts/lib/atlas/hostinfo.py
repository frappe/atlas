"""Host signature — is this host the one a warm snapshot was captured on?

A Firecracker memory snapshot is only restorable on a matching CPU model (CPUID
feature set), host kernel, and Firecracker build; Intel↔AMD is unsupported and
even same-size DigitalOcean droplets can differ (the Premium tier guarantees one
of the latest *two* CPU generations, and live migration can move a droplet to a
different physical host). So warm-snapshot-vm.py records this signature at
capture and vm-restore.py refuses to load when the live host's differs —
cold-booting instead, which is always correct.

The parse functions are pure (text in, dict out) so they unit-test with fixture
text on any machine; only `host_signature()` touches the live host (procfs,
uname, the firecracker binary).
"""

from __future__ import annotations

import hashlib
import platform
import subprocess

CPUINFO_PATH = "/proc/cpuinfo"
FIRECRACKER_BINARY = "/usr/local/bin/firecracker"


def parse_cpu_signature(cpuinfo_text: str) -> dict:
	"""The CPU identity facts from /proc/cpuinfo's first processor block. The
	flags set is the actual CPUID surface a snapshot depends on; it is large, so
	it is stored hashed — equality is all the comparison needs. `microcode` is
	included because an update can change CPUID-visible behaviour; absent on
	some virtualised hosts, recorded as ""."""
	model = ""
	microcode = ""
	flags = ""
	for line in cpuinfo_text.splitlines():
		if not line.strip():
			break  # end of the first processor block; the rest repeat it
		key, _, value = line.partition(":")
		key, value = key.strip(), value.strip()
		if key == "model name" and not model:
			model = value
		elif key == "microcode" and not microcode:
			microcode = value
		elif key == "flags" and not flags:
			flags = value
	flags_digest = hashlib.sha256(" ".join(sorted(flags.split())).encode()).hexdigest()[:16]
	return {"cpu_model": model, "microcode": microcode, "cpu_flags_sha256": flags_digest}


def parse_firecracker_version(output: str) -> str:
	"""The version token from `firecracker --version` ("Firecracker v1.15.1" →
	"v1.15.1"). Falls back to the whole first line on an unexpected shape — the
	comparison is equality, so any stable string works."""
	first_line = output.strip().splitlines()[0] if output.strip() else ""
	tokens = first_line.split()
	for token in tokens:
		if token.startswith("v") and token[1:2].isdigit():
			return token
	return first_line


def host_signature() -> dict:
	"""The live host's signature: CPU identity + kernel + Firecracker version.
	Compared by plain dict equality against the captured one."""
	with open(CPUINFO_PATH) as handle:
		signature = parse_cpu_signature(handle.read())
	signature["kernel"] = platform.release()
	version = subprocess.run([FIRECRACKER_BINARY, "--version"], capture_output=True, text=True)
	signature["firecracker"] = parse_firecracker_version(version.stdout)
	return signature
