#!/usr/bin/env python3
"""Readability lint delta-gate (the P1 'adopt the standard' phase of the Atlas refactor).

It runs the complexity / size gates that are deliberately kept OUT of the enforced
ruff `select` in pyproject.toml — adding them there would fail CI on the whole
pre-existing backlog (see llm/atlas-refactor-findings.md §8). Instead this gate
compares the current violations against a checked-in baseline and fails ONLY on
NEW ones. The backlog is frozen and burned down file by file; it can shrink freely
but never grow. When a file gets better, re-run with --update-baseline to ratchet.

Higher-signal gates first (per the survey in findings §6: nesting and cognitive
complexity carry more signal than raw cyclomatic, which is mostly a size confound):

  - ruff complexity   C901, PLR0911/0912/0913/0915, and PLR1702 nesting (--preview)
  - module length     > 500 soft target / > 1000 hard (no ruff rule; a line count)
  - core -> services   import-direction check (INERT until the P3 split makes the
                       core/ and services/ dirs; then it enforces the one-way rule)
  - OPTIONAL, run only when the tool is installed (the CI job installs them; a local
    offline run skips them and says so):
        radon mi   maintainability index below rank A
        flake8     cognitive complexity CCR001 (> 15)

Usage:
    python .github/scripts/lint_gate.py                 # gate: exit 1 on NEW violations
    python .github/scripts/lint_gate.py --update-baseline   # re-record the baseline
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# Run from the app root (the dir holding pyproject.toml + the atlas/ package).
APP_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "atlas"  # scan the whole app subtree; test files are excluded below.
BASELINE_PATH = Path(__file__).resolve().parent / "lint_baseline.json"

# Thresholds — the standard from the plan / findings §6. Kept here (not in the
# enforced pyproject select) so the enforced linter never sees them.
RUFF_COMPLEXITY_RULES = ["C901", "PLR0911", "PLR0912", "PLR0913", "PLR0915"]
RUFF_THRESHOLDS = [
	"lint.mccabe.max-complexity=10",  # C901 cyclomatic
	"lint.pylint.max-args=5",  # PLR0913
	"lint.pylint.max-branches=12",  # PLR0912
	"lint.pylint.max-statements=50",  # PLR0915
	"lint.pylint.max-returns=6",  # PLR0911 (advisory — loose for guard clauses)
	"lint.pylint.max-nested-blocks=4",  # PLR1702 (nesting; needs --preview)
]
MODULE_LENGTH_TARGET = 500
MODULE_LENGTH_HARD = 1000


def _is_test(path: str) -> bool:
	"""Tests are excluded from the standard — they legitimately fan out."""
	return "/tests/" in path or Path(path).name.startswith("test_")


def _rel(path: str) -> str:
	"""A stable, machine-independent key: path relative to the app root."""
	try:
		return str(Path(path).resolve().relative_to(APP_ROOT))
	except ValueError:
		return path


def _ruff(select: list[str], preview: bool) -> list[dict]:
	cmd = ["ruff", "check", PACKAGE, "--select", ",".join(select), "--output-format", "json"]
	for cfg in RUFF_THRESHOLDS:
		cmd += ["--config", cfg]
	if preview:
		cmd.append("--preview")
	# ruff exits non-zero when it finds violations; that is expected, not an error.
	out = subprocess.run(cmd, cwd=APP_ROOT, capture_output=True, text=True).stdout
	return json.loads(out or "[]")


def ruff_complexity() -> dict[str, int]:
	"""Per-(file, rule) violation counts — the unit we ratchet. A wholly new
	(file, rule) pair, or a higher count for an existing one, is a NEW violation."""
	rows = _ruff(RUFF_COMPLEXITY_RULES, preview=False) + _ruff(["PLR1702"], preview=True)
	counts: dict[str, int] = {}
	for row in rows:
		if _is_test(row["filename"]):
			continue
		key = f"{_rel(row['filename'])}::{row['code']}"
		counts[key] = counts.get(key, 0) + 1
	return counts


def module_length() -> dict[str, int]:
	"""Line count for every non-test .py over the soft target. A file that crosses
	the target, or grows past its baseline length, is a NEW violation; shrinking is
	always allowed. Files over the hard limit are flagged in the report."""
	lengths: dict[str, int] = {}
	for path in sorted((APP_ROOT / PACKAGE).rglob("*.py")):
		p = str(path)
		if _is_test(p):
			continue
		n = sum(1 for _ in path.open("rb"))
		if n > MODULE_LENGTH_TARGET:
			lengths[_rel(p)] = n
	return lengths


def core_services_imports() -> list[str]:
	"""The one-way rule: core/ must never import services/. Inert until P3 creates
	those dirs (returns []), then it lists every offending `file -> import` edge."""
	core = APP_ROOT / PACKAGE / "atlas" / "core"
	if not core.is_dir():
		return []
	bad: list[str] = []
	for path in sorted(core.rglob("*.py")):
		text = path.read_text(encoding="utf-8", errors="replace")
		for line in text.splitlines():
			s = line.strip()
			if s.startswith(("import ", "from ")) and "atlas.atlas.services" in s:
				bad.append(f"{_rel(str(path))} -> {s}")
	return bad


def radon_mi() -> dict[str, str] | None:
	"""Maintainability-index rank per file (radon), when radon is installed. Any
	file below rank A is a violation. None when radon is absent (offline run)."""
	if not shutil.which("radon"):
		return None
	out = subprocess.run(
		["radon", "mi", PACKAGE, "--min", "B", "--json"],
		cwd=APP_ROOT,
		capture_output=True,
		text=True,
	).stdout
	data = json.loads(out or "{}")
	return {_rel(f): info["rank"] for f, info in data.items() if not _is_test(f)}


def cognitive() -> dict[str, int] | None:
	"""Per-file cognitive-complexity (CCR001) violation counts via flake8 +
	flake8-cognitive-complexity, when installed. None when absent (offline run)."""
	if not shutil.which("flake8"):
		return None
	out = subprocess.run(
		["flake8", PACKAGE, "--select=CCR001", "--max-cognitive-complexity=15"],
		cwd=APP_ROOT,
		capture_output=True,
		text=True,
	).stdout
	counts: dict[str, int] = {}
	for line in out.splitlines():
		# flake8 default format: path:line:col: CCR001 message
		file = line.split(":", 1)[0]
		if file and not _is_test(file):
			counts[_rel(file)] = counts.get(_rel(file), 0) + 1
	return counts


def collect() -> dict:
	"""Everything the gate measures now. Optional tools that are absent are omitted
	entirely (not recorded as zero) so the baseline stays honest about what ran."""
	data = {
		"ruff_complexity": ruff_complexity(),
		"module_length": module_length(),
		"core_services_imports": core_services_imports(),
	}
	mi = radon_mi()
	if mi is not None:
		data["radon_mi"] = mi
	cc = cognitive()
	if cc is not None:
		data["cognitive"] = cc
	return data


def new_violations(current: dict, baseline: dict) -> list[str]:
	"""What got WORSE relative to the baseline — the only thing that fails the gate."""
	news: list[str] = []

	# Counted categories: a higher (or brand-new) count is a regression.
	for cat in ("ruff_complexity", "cognitive"):
		base = baseline.get(cat, {})
		for key, n in current.get(cat, {}).items():
			if n > base.get(key, 0):
				news.append(f"[{cat}] {key}: {base.get(key, 0)} -> {n}")

	# Module length: a file that crosses the target, or grows past its baseline.
	base_len = baseline.get("module_length", {})
	for file, n in current.get("module_length", {}).items():
		prior = base_len.get(file)
		if prior is None:
			news.append(f"[module_length] {file}: newly over {MODULE_LENGTH_TARGET} ({n} lines)")
		elif n > prior:
			news.append(f"[module_length] {file}: grew {prior} -> {n} lines")

	# MI: a file dropping below its baseline rank (A < B < C ...).
	base_mi = baseline.get("radon_mi", {})
	for file, rank in current.get("radon_mi", {}).items():
		prior = base_mi.get(file, "A")
		if rank > prior:  # ranks are letters; "B" > "A" means worse
			news.append(f"[radon_mi] {file}: rank {prior} -> {rank}")

	# The one-way import rule is absolute — any edge is a violation.
	for edge in current.get("core_services_imports", []):
		if edge not in baseline.get("core_services_imports", []):
			news.append(f"[core->services import] {edge}")

	return news


def summarize(current: dict) -> None:
	ran = [k for k in ("radon_mi", "cognitive") if k in current]
	skipped = [k for k in ("radon_mi", "cognitive") if k not in current]
	over_hard = sorted((n, f) for f, n in current.get("module_length", {}).items() if n > MODULE_LENGTH_HARD)
	print(f"  ruff complexity backlog : {sum(current['ruff_complexity'].values())} violations")
	print(
		f"  modules over {MODULE_LENGTH_TARGET:<4}       : {len(current['module_length'])}"
		f" ({len(over_hard)} over the {MODULE_LENGTH_HARD} hard limit)"
	)
	if skipped:
		print(f"  skipped (not installed) : {', '.join(skipped)} — run in CI for full coverage")
	if ran:
		print(f"  ran optional tools      : {', '.join(ran)}")


def main() -> int:
	ap = argparse.ArgumentParser(description=__doc__)
	ap.add_argument("--update-baseline", action="store_true", help="re-record the baseline")
	args = ap.parse_args()

	current = collect()

	if args.update_baseline:
		BASELINE_PATH.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
		print(f"baseline written to {_rel(str(BASELINE_PATH))}")
		summarize(current)
		return 0

	if not BASELINE_PATH.exists():
		print(f"no baseline at {_rel(str(BASELINE_PATH))}; run --update-baseline first", file=sys.stderr)
		return 2

	baseline = json.loads(BASELINE_PATH.read_text())
	news = new_violations(current, baseline)
	print("Readability lint gate (fail only on NEW violations vs the baseline):")
	summarize(current)
	if news:
		print(f"\n{len(news)} NEW violation(s) — fix them or, if intentional, --update-baseline:")
		for n in news:
			print(f"  + {n}")
		return 1
	print("\nOK — no new violations; the backlog did not grow.")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
