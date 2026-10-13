"""Render the pull request stats comment from a diff and coverage reports."""

import argparse
import re
import subprocess
import xml.etree.ElementTree as ElementTree
from collections import defaultdict
from pathlib import Path

MARKER = "<!-- atlas-pr-stats -->"
MODULES = {
	"Atlas": re.compile(r"^(atlas|realtime)/"),
	"Metal": re.compile(r"^metal/"),
	"HTTP proxy": re.compile(r"^services/http-proxy/"),
	"IPv6 router": re.compile(r"^services/ipv6-router/"),
	"WG Mesh": re.compile(r"^services/wg-mesh/"),
	"WG gateway": re.compile(r"^services/wg-gateway/"),
	"Control cluster": re.compile(r"^services/control-cluster/"),
}
TEST_FILE = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*\.py$|_test\.(py|go)$")
LOCK_FILE = re.compile(r"(^|/)(uv\.lock|package-lock\.json|yarn\.lock|go\.sum)$")
GO_MODULE = "github.com/frappe/atlas/"
HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")

# A coverage report maps each instrumented file to {line: is_covered}.
LineCoverage = dict[str, dict[int, bool]]


def git(*args: str) -> str:
	return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def get_changed_files(base: str, head: str) -> list[tuple[str, int, int]]:
	files = []
	for line in git("diff", "--numstat", "--no-renames", base, head).splitlines():
		added, removed, path = line.split("\t", 2)
		if added != "-":
			files.append((path, int(added), int(removed)))
	return files


def get_excluded_files(paths: list[str], head: str) -> set[str]:
	attributes = git("check-attr", "--source", head, "linguist-generated", "--", *paths) if paths else ""
	generated = {line.split(": ")[0] for line in attributes.splitlines() if line.endswith(": true")}
	return generated | {path for path in paths if LOCK_FILE.search(path)}


def get_added_lines(base: str, head: str) -> dict[str, set[int]]:
	added, path = defaultdict(set), None
	for line in git("diff", "--unified=0", "--no-renames", base, head).splitlines():
		if line.startswith("+++ "):
			path = line[6:] if line.startswith("+++ b/") else None
		elif path and (hunk := HUNK.match(line)):
			start, count = int(hunk[1]), int(hunk[2] or 1)
			added[path].update(range(start, start + count))
	return added


def read_python_coverage(report: Path) -> tuple[float, LineCoverage]:
	root = ElementTree.parse(report).getroot()
	lines = {
		element.get("filename"): {
			int(line.get("number")): int(line.get("hits")) > 0 for line in element.iter("line")
		}
		for element in root.iter("class")
	}
	return float(root.get("line-rate")) * 100, lines


def read_go_coverage(report: Path) -> tuple[float, LineCoverage]:
	blocks = {}
	for line in report.read_text().splitlines()[1:]:
		block, statements, count = line.rsplit(" ", 2)
		previous = blocks.get(block, (0, 0))[1]
		blocks[block] = (int(statements), max(previous, int(count)))

	lines = defaultdict(dict)
	for block, (_, count) in blocks.items():
		match = re.match(r"(.+):(\d+)\.\d+,(\d+)\.\d+$", block)
		path = match[1].removeprefix(GO_MODULE)
		for number in range(int(match[2]), int(match[3]) + 1):
			lines[path][number] = lines[path].get(number, False) or count > 0

	total = sum(statements for statements, _ in blocks.values())
	covered = sum(statements for statements, count in blocks.values() if count > 0)
	return covered * 100 / total if total else 0.0, lines


def get_module(path: str) -> str:
	return next((name for name, pattern in MODULES.items() if pattern.search(path)), "Other")


def render_changes(files: list[tuple[str, int, int]]) -> list[str]:
	totals = defaultdict(lambda: [0, 0, 0, 0])
	for path, added, removed in files:
		offset = 2 if TEST_FILE.search(path) else 0
		totals[get_module(path)][offset] += added
		totals[get_module(path)][offset + 1] += removed

	rows = [name for name in [*MODULES, "Other"] if name in totals]
	total = [sum(totals[name][index] for name in rows) for index in range(4)]
	added, removed = total[0] + total[2], total[1] + total[3]
	test_share = f"{total[2] * 100 // added}%" if added else "0%"

	lines = [
		"### Change summary",
		"",
		f"**+{added} / -{removed}** lines in {len(files)} files · tests are {test_share} of added lines",
		"",
		"| Module | Code + | Code - | Tests + | Tests - |",
		"|---|---:|---:|---:|---:|",
	]
	lines += [f"| {name} | " + " | ".join(map(str, totals[name])) + " |" for name in rows]
	lines.append("| **Total** | " + " | ".join(f"**{value}**" for value in total) + " |")
	return lines


def render_coverage_row(
	name: str, base: float | None, head: tuple[float, LineCoverage] | None, added: dict[str, set[int]]
) -> str:
	base_text = f"{base:.1f}%" if base is not None else "—"
	if head is None:
		return f"| {name} | {base_text} | not run | | |"

	percent, lines = head
	change = ""
	if base is not None:
		delta = round(percent, 1) - round(base, 1)
		change = f"{'🟢' if delta >= 0 else '🔴'} {delta:+.1f}%"

	changed = [
		lines[path][number]
		for path, numbers in added.items()
		if not TEST_FILE.search(path) and path in lines
		for number in numbers
		if number in lines[path]
	]
	changed_text = (
		f"{sum(changed) * 100 // len(changed)}% ({sum(changed)} of {len(changed)})" if changed else "—"
	)
	return f"| {name} | {base_text} | {percent:.1f}% | {change} | {changed_text} |"


def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument("--base", required=True)
	parser.add_argument("--head", required=True)
	parser.add_argument("--base-branch", required=True)
	parser.add_argument("--coverage", type=Path, required=True, help="Folder with head/ and base/ reports")
	arguments = parser.parse_args()

	files = get_changed_files(arguments.base, arguments.head)
	excluded = get_excluded_files([path for path, _, _ in files], arguments.head)
	files = [file for file in files if file[0] not in excluded]
	added = {
		path: numbers
		for path, numbers in get_added_lines(arguments.base, arguments.head).items()
		if path not in excluded
	}

	lines = [
		MARKER,
		*render_changes(files),
		"",
		"### Coverage",
		"",
		f"| Module | {arguments.base_branch} | This PR | Change | Changed lines covered |",
		"|---|---:|---:|---:|---:|",
	]
	for name, report, reader in (
		("Atlas", "atlas-coverage/coverage.xml", read_python_coverage),
		("Metal", "metal-coverage/coverage.out", read_go_coverage),
	):
		base_report, head_report = arguments.coverage / "base" / report, arguments.coverage / "head" / report
		base = reader(base_report)[0] if base_report.exists() else None
		head = reader(head_report) if head_report.exists() else None
		lines.append(render_coverage_row(name, base, head, added))

	lines += [
		"",
		f"<sub>Updated for {arguments.head[:7]} · Generated files, API clients and lock files excluded</sub>",
	]
	print("\n".join(lines))


if __name__ == "__main__":
	main()
