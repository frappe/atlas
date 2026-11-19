#!/usr/bin/env python3
"""Per-VM traffic baselines from Metal's 10 s metric samples, for abuse threshold calibration.

collect: run once on each Metal host, as root (VM directories are 0700). Reads only.
Writes one JSON file (mode 0600) to /tmp. It holds VM ids and rates, no addresses or content.
    sudo nice -n 19 ionice -c3 python3 -I atlas-abuse-baseline.py collect
merge: run anywhere on the collected files, one per host. Prints a report, optionally writes a CSV.
    python3 -I atlas-abuse-baseline.py merge abuse-baseline-*.json --csv fleet.csv
"""

import argparse
import csv
import itertools
import json
import math
import os
import socket
import sys
import time
from array import array
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_MACHINES_DIRECTORY = "/var/lib/metal/machines"
SAMPLE_INTERVAL_SECONDS = 10
MAXIMUM_INTERVAL_SECONDS = 15
RATIO_FLOOR_PACKETS_PER_SECOND = 1000

COUNTERS = (
	"sent_packets",
	"sent_bytes",
	"sent_udp_packets",
	"sent_icmp_packets",
	"sent_tcp_syn_packets",
	"sent_tcp_rst_packets",
	"received_packets",
	"received_bytes",
	"cpu_usage_microseconds",
)

METRICS = (
	"sent_pps",
	"sent_udp_pps",
	"sent_icmp_pps",
	"sent_syn_per_second",
	"sent_rst_per_second",
	"received_pps",
	"sent_bits_per_second",
	"received_bits_per_second",
	"sent_average_packet_bytes",
	"received_to_sent_packet_ratio",
	"cpu_percent_of_quota",
)

PERCENTILES = (("p1", 1.0), ("p10", 10.0), ("p50", 50.0), ("p90", 90.0), ("p99", 99.0), ("p999", 99.9))

# Starting values from the Watcher thresholds:
# https://github.com/frappe/cargo/blob/develop/docs/watcher.md#thresholds
# The planned counters are in
# docs/abuse/metrics.md#counters. Shape conditions that need counters Metal does not have yet (answer
# ratios, public/private split) are left out, so a hit here is an upper bound on what the real rule would do.
RULES = {
	"udp_flood": (12, lambda rate: rate["sent_udp_pps"] > 20_000),
	"small_packet_flood": (
		12,
		lambda rate: rate["sent_pps"] > 20_000 and rate["sent_average_packet_bytes"] < 200,
	),
	"volumetric": (
		6,
		lambda rate: rate["sent_bits_per_second"] > 1e9 and (rate["received_to_sent_packet_ratio"] or 0) < 0.05,
	),
	"icmp_flood": (12, lambda rate: rate["sent_icmp_pps"] > 1_000),
	"syn_flood": (12, lambda rate: rate["sent_syn_per_second"] > 2_000),
	"inbound_notice": (
		12,
		lambda rate: rate["received_pps"] > 20_000 or rate["received_bits_per_second"] > 1e9,
	),
}

RULE_NOTES = {
	"udp_flood": "volume floor only: no received-UDP counter yet",
	"small_packet_flood": "includes mesh traffic",
	"volumetric": "includes mesh traffic",
	"icmp_flood": "includes mesh traffic",
	"syn_flood": "SYN count includes SYN-ACK replies (track_traffic.c:137)",
	"inbound_notice": "includes mesh traffic",
}


def main():
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	commands = parser.add_subparsers(dest="command", required=True)

	collect_parser = commands.add_parser("collect", help="summarise the VMs on this host")
	collect_parser.add_argument("--machines-dir", default=DEFAULT_MACHINES_DIRECTORY)
	collect_parser.add_argument("--output", help="default: /tmp/abuse-baseline-<host>-<UTC time>.json")

	merge_parser = commands.add_parser("merge", help="combine collected files into a fleet report")
	merge_parser.add_argument("inputs", nargs="+")
	merge_parser.add_argument("--csv", help="also write one row per VM to this file")

	arguments = parser.parse_args()
	if arguments.command == "collect":
		collect(Path(arguments.machines_dir), arguments.output)
	else:
		merge([Path(path) for path in arguments.inputs], arguments.csv)


def collect(machines_directory, output):
	if not machines_directory.is_dir():
		sys.exit(f"{machines_directory} is not a directory")
	host = socket.gethostname()
	output_path = Path(output or f"/tmp/abuse-baseline-{host}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json")
	if machines_directory.resolve() in output_path.resolve().parents:
		sys.exit("refusing to write inside the machines directory")

	summaries = []
	failures = []
	for directory in sorted(machines_directory.iterdir()):
		if not (directory.is_dir() and (directory / "metrics").is_dir()):
			continue
		try:
			summaries.append(summarise_virtual_machine(directory))
		except Exception as error:
			failures.append({"virtual_machine": directory.name, "error": f"{type(error).__name__}: {error}"})

	report = {
		"schema_version": SCHEMA_VERSION,
		"host": host,
		"collected_at": int(time.time()),
		"machines_directory": str(machines_directory),
		"virtual_machines": summaries,
		"failures": failures,
	}
	descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
	with os.fdopen(descriptor, "w") as file:
		json.dump(report, file, indent=1)
	print(f"{len(summaries)} VMs summarised, {len(failures)} failed, written to {output_path}")
	for failure in failures:
		print(f"  {failure['virtual_machine']}: {failure['error']}", file=sys.stderr)


def summarise_virtual_machine(directory):
	records, malformed_lines, unreadable_files = read_records(directory / "metrics")
	cpu_millicores = read_cpu_millicores(directory)
	series = {name: array("d") for name in METRICS}
	peak_times = {name: (-math.inf, None) for name in METRICS}
	rule_runs = {name: RuleRun() for name in RULES}
	daily = {}
	resets = gaps = intervals = 0

	for previous, current in itertools.pairwise(records):
		elapsed = current["timestamp"] - previous["timestamp"]
		if not previous["up"] or not current["up"] or elapsed <= 0 or elapsed > MAXIMUM_INTERVAL_SECONDS:
			gaps += 1
			end_runs(rule_runs)
			continue
		deltas = {name: current[name] - previous[name] for name in COUNTERS}
		if any(delta < 0 for delta in deltas.values()):
			resets += 1
			end_runs(rule_runs)
			continue

		intervals += 1
		rate = get_rates(deltas, elapsed, cpu_millicores)
		for name, value in rate.items():
			if value is None:
				continue
			series[name].append(value)
			if value > peak_times[name][0]:
				peak_times[name] = (value, current["timestamp"])
		for name, (sustain_intervals, matches) in RULES.items():
			rule_runs[name].observe(matches(rate), elapsed, sustain_intervals * SAMPLE_INTERVAL_SECONDS)
		day = time.strftime("%Y-%m-%d", time.gmtime(current["timestamp"]))
		maxima = daily.setdefault(day, {"sent_pps_max": 0.0, "sent_syn_per_second_max": 0.0})
		maxima["sent_pps_max"] = max(maxima["sent_pps_max"], rate["sent_pps"])
		maxima["sent_syn_per_second_max"] = max(maxima["sent_syn_per_second_max"], rate["sent_syn_per_second"])
	end_runs(rule_runs)

	return {
		"virtual_machine": directory.name,
		"cpu_millicores": cpu_millicores,
		"first_sample": records[0]["timestamp"] if records else None,
		"last_sample": records[-1]["timestamp"] if records else None,
		"samples": len(records),
		"intervals": intervals,
		"counter_resets": resets,
		"gaps": gaps,
		"malformed_lines": malformed_lines,
		"unreadable_files": unreadable_files,
		"metrics": {name: get_distribution(series[name], peak_times[name][1]) for name in METRICS},
		"rules": {name: run.as_dict() for name, run in rule_runs.items()},
		"daily": daily,
	}


def read_records(metrics_directory):
	"""Records in timestamp order, first of each timestamp kept. A partial last line counts as malformed."""
	records = {}
	malformed_lines = unreadable_files = 0
	for path in sorted(metrics_directory.glob("*.jsonl")):
		try:
			file = open(path, encoding="utf-8", errors="replace", opener=open_without_access_time)
		except OSError:
			# Metald prunes old days while this runs.
			unreadable_files += 1
			continue
		with file:
			for line in file:
				try:
					record = parse_record(line)
				except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
					malformed_lines += 1
					continue
				records.setdefault(record["timestamp"], record)
	return [records[timestamp] for timestamp in sorted(records)], malformed_lines, unreadable_files


def parse_record(line):
	stored = json.loads(line)
	sample = stored["metrics"]
	if not isinstance(sample, dict):
		raise TypeError("metrics is not an object")
	record = {"timestamp": int(stored["timestamp"]), "up": bool(sample.get("up"))}
	for name in COUNTERS:
		record[name] = int(sample.get(name, 0))
	return record


def open_without_access_time(path, flags):
	"""Reading must not update access times on the host. O_NOATIME needs the file owner or root."""
	try:
		return os.open(path, flags | getattr(os, "O_NOATIME", 0))
	except PermissionError:
		return os.open(path, flags)


def read_cpu_millicores(directory):
	"""Only the CPU quota is read from config.json. It also holds user data and keys: keep nothing else."""
	try:
		with open(directory / "config.json", encoding="utf-8", opener=open_without_access_time) as file:
			value = json.load(file)["specification"]["cpu_millicores"]
	except (OSError, ValueError, KeyError, TypeError):
		return None
	return value if isinstance(value, int) and value > 0 else None


def get_rates(deltas, elapsed, cpu_millicores):
	sent_pps = deltas["sent_packets"] / elapsed
	received_pps = deltas["received_packets"] / elapsed
	return {
		"sent_pps": sent_pps,
		"sent_udp_pps": deltas["sent_udp_packets"] / elapsed,
		"sent_icmp_pps": deltas["sent_icmp_packets"] / elapsed,
		"sent_syn_per_second": deltas["sent_tcp_syn_packets"] / elapsed,
		"sent_rst_per_second": deltas["sent_tcp_rst_packets"] / elapsed,
		"received_pps": received_pps,
		"sent_bits_per_second": deltas["sent_bytes"] * 8 / elapsed,
		"received_bits_per_second": deltas["received_bytes"] * 8 / elapsed,
		"sent_average_packet_bytes": (
			deltas["sent_bytes"] / deltas["sent_packets"] if deltas["sent_packets"] else None
		),
		"received_to_sent_packet_ratio": (
			received_pps / sent_pps if sent_pps >= RATIO_FLOOR_PACKETS_PER_SECOND else None
		),
		"cpu_percent_of_quota": (
			deltas["cpu_usage_microseconds"] / (elapsed * cpu_millicores * 1000) * 100 if cpu_millicores else None
		),
	}


def get_distribution(values, peak_time):
	if not values:
		return None
	ordered = sorted(values)
	distribution = {label: round(get_percentile(ordered, percentile), 3) for label, percentile in PERCENTILES}
	distribution["max"] = round(ordered[-1], 3)
	distribution["max_at"] = peak_time
	distribution["count"] = len(ordered)
	return distribution


def get_percentile(ordered, percentile):
	"""Nearest rank on a sorted list."""
	rank = max(1, math.ceil(percentile / 100 * len(ordered)))
	return ordered[rank - 1]


class RuleRun:
	"""Counts intervals over a threshold, and unbroken runs long enough to fire the rule."""

	def __init__(self):
		self.intervals_over = 0
		self.sustained_runs = 0
		self.longest_run_seconds = 0
		self.current_seconds = 0
		self.sustain_seconds = None

	def observe(self, is_over, elapsed, sustain_seconds):
		self.sustain_seconds = sustain_seconds
		if not is_over:
			self.end()
			return
		self.intervals_over += 1
		self.current_seconds += elapsed

	def end(self):
		if self.sustain_seconds and self.current_seconds >= self.sustain_seconds:
			self.sustained_runs += 1
		self.longest_run_seconds = max(self.longest_run_seconds, self.current_seconds)
		self.current_seconds = 0

	def as_dict(self):
		return {
			"intervals_over": self.intervals_over,
			"sustained_runs": self.sustained_runs,
			"longest_run_seconds": self.longest_run_seconds,
		}


def end_runs(rule_runs):
	for run in rule_runs.values():
		run.end()


def merge(paths, csv_path):
	entries = []
	failures = []
	hosts = {}
	for path in paths:
		with open(path, encoding="utf-8") as file:
			report = json.load(file)
		if report.get("schema_version") != SCHEMA_VERSION:
			sys.exit(f"{path}: schema version {report.get('schema_version')}, expected {SCHEMA_VERSION}")
		if report["host"] in hosts:
			sys.exit(f"{path}: host {report['host']} already read from {hosts[report['host']]}. Pass one file per host.")
		hosts[report["host"]] = path
		for summary in report["virtual_machines"]:
			entries.append((report["host"], summary))
		failures += [(report["host"], failure) for failure in report.get("failures", [])]

	print_coverage(paths, entries, failures)
	print_metric_table(entries)
	print_rule_table(entries)
	print_daily_table(entries)
	if csv_path:
		if os.path.exists(csv_path):
			sys.exit(f"{csv_path} already exists")
		write_csv(csv_path, entries)
		print(f"\nPer-VM rows written to {csv_path}")


def print_coverage(paths, entries, failures):
	sampled = [summary for _, summary in entries if summary["first_sample"]]
	names = [summary["virtual_machine"] for _, summary in entries]
	duplicates = len(names) - len(set(names))
	print(f"# Fleet baseline\n\n{len(paths)} hosts, {len(entries)} VM entries, {duplicates} VMs seen on more than one host (migrated).")
	for host, failure in failures:
		print(f"Not summarised: {failure['virtual_machine']} on {host}: {failure['error']}")
	if sampled:
		first = min(summary["first_sample"] for summary in sampled)
		last = max(summary["last_sample"] for summary in sampled)
		print(f"Samples from {format_time(first)} to {format_time(last)}.")
	resets = sum(summary["counter_resets"] for _, summary in entries)
	malformed = sum(summary["malformed_lines"] for _, summary in entries)
	unreadable = sum(summary["unreadable_files"] for _, summary in entries)
	print(f"{resets} counter resets, {malformed} malformed lines and {unreadable} unreadable files skipped.")
	print("All traffic numbers include mesh traffic. SYN counts include SYN-ACK replies.")
	print("received_to_sent_packet_ratio only covers intervals with at least 1000 sent pps.")


def print_metric_table(entries):
	print("\n## Per-VM distributions\n")
	print("Each VM gives one p99, one max and one p1. The columns describe those values across the fleet.")
	print("Lowest p1 matters for the packet size and answer ratio rules, which fire on low values.\n")
	print("| Metric | VMs | Median of p99 | p90 of p99 | Highest p99 | Median of max | Highest max | Highest max on | Lowest p1 |")
	print("|---|---|---|---|---|---|---|---|---|")
	for name in METRICS:
		rows = [(host, summary, summary["metrics"][name]) for host, summary in entries if summary["metrics"][name]]
		if not rows:
			print(f"| {name} | 0 | | | | | | | |")
			continue
		p99s = sorted(distribution["p99"] for _, _, distribution in rows)
		maxima = sorted(distribution["max"] for _, _, distribution in rows)
		host, summary, top = max(rows, key=lambda row: row[2]["max"])
		peak = f"{summary['virtual_machine']} on {host}, {format_time(top['max_at'])}" if maxima[-1] else ""
		print(
			f"| {name} | {len(rows)} | {format_number(get_percentile(p99s, 50))} | "
			f"{format_number(get_percentile(p99s, 90))} | {format_number(p99s[-1])} | "
			f"{format_number(get_percentile(maxima, 50))} | {format_number(maxima[-1])} | "
			f"{peak} | {format_number(min(distribution['p1'] for _, _, distribution in rows))} |"
		)


def print_rule_table(entries):
	print("\n## Starting thresholds against this data\n")
	print("A sustained run is long enough to fire the rule. Every hit here is a possible false positive to explain.\n")
	print("| Rule | VMs over at least once | VMs with a sustained run | Sustained runs | Longest run (s) | Top VMs | Caveat |")
	print("|---|---|---|---|---|---|---|")
	for name in RULES:
		results = [(summary["virtual_machine"], summary["rules"][name]) for _, summary in entries]
		over = [result for result in results if result[1]["intervals_over"]]
		sustained = sorted(
			(result for result in results if result[1]["sustained_runs"]),
			key=lambda result: result[1]["sustained_runs"],
			reverse=True,
		)
		total_runs = sum(result[1]["sustained_runs"] for result in sustained)
		longest = max((result[1]["longest_run_seconds"] for result in results), default=0)
		top = ", ".join(f"{vm} ({rule['sustained_runs']})" for vm, rule in sustained[:5])
		print(f"| {name} | {len(over)} | {len(sustained)} | {total_runs} | {longest} | {top} | {RULE_NOTES[name]} |")


def print_daily_table(entries):
	days = {}
	for _, summary in entries:
		for day, maxima in summary["daily"].items():
			fleet = days.setdefault(day, {"sent_pps_max": 0.0, "sent_syn_per_second_max": 0.0})
			for key in fleet:
				fleet[key] = max(fleet[key], maxima[key])
	print("\n## Fleet maxima per UTC day\n")
	print("Look for month-end bursts (payroll and invoice email).\n")
	print("| Day | Highest sent pps | Highest SYN/s |")
	print("|---|---|---|")
	for day in sorted(days):
		print(f"| {day} | {format_number(days[day]['sent_pps_max'])} | {format_number(days[day]['sent_syn_per_second_max'])} |")


def write_csv(csv_path, entries):
	header = ["host", "virtual_machine", "cpu_millicores", "intervals", "counter_resets"]
	for name in METRICS:
		header += [f"{name}_p99", f"{name}_max"]
	header += [f"{name}_sustained_runs" for name in RULES]
	with open(csv_path, "x", newline="", encoding="utf-8") as file:
		writer = csv.writer(file)
		writer.writerow(header)
		for host, summary in entries:
			row = [host, summary["virtual_machine"], summary["cpu_millicores"], summary["intervals"], summary["counter_resets"]]
			for name in METRICS:
				distribution = summary["metrics"][name] or {}
				row += [distribution.get("p99"), distribution.get("max")]
			row += [summary["rules"][name]["sustained_runs"] for name in RULES]
			writer.writerow(row)


def format_number(value):
	if value >= 1e9:
		return f"{value / 1e9:.2f}G"
	if value >= 1e6:
		return f"{value / 1e6:.2f}M"
	if value >= 1e4:
		return f"{value / 1e3:.1f}k"
	return f"{value:.2f}".rstrip("0").rstrip(".")


def format_time(timestamp):
	return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(timestamp)) if timestamp else ""


if __name__ == "__main__":
	main()
