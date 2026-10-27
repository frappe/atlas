import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPOSITORY = "frappe/atlas"
SCRIPTS = "scripts/insights"


def find_bench(site: str) -> Path:
	"""Return the Pilot bench that holds site."""
	pilot = shutil.which("pilot")
	if not pilot:
		sys.exit("error: pilot is not on PATH")

	benches = Path(os.path.realpath(pilot)).parent.parent / "benches"
	matches = [path.parent.parent.parent for path in benches.glob(f"*/sites/{site}/site_config.json")]
	if len(matches) != 1:
		sys.exit(f"error: {len(matches)} benches under {benches} have the site {site}")
	return matches[0]


def download_scripts(ref: str, directory: Path) -> Path:
	"""Extract scripts/insights of the Atlas repository at ref into directory."""
	url = f"https://codeload.github.com/{REPOSITORY}/tar.gz/{ref}"
	with (
		urllib.request.urlopen(url, timeout=300) as response,
		tarfile.open(fileobj=response, mode="r|gz") as archive,
	):
		for member in archive:
			# GitHub puts every path under one "<repository>-<ref>/" folder.
			member.name = member.name.partition("/")[2]
			if member.isfile() and member.name.startswith(f"{SCRIPTS}/"):
				archive.extract(member, directory, filter="data")

	scripts = directory / SCRIPTS
	if not (scripts / "generate_workbook.py").exists():
		sys.exit(f"error: {REPOSITORY}@{ref} has no {SCRIPTS}/generate_workbook.py")
	return scripts


def main() -> None:
	parser = argparse.ArgumentParser(
		description="Generate the Atlas workbook and update it on an Insights site of a Pilot or Frappe bench."
	)
	parser.add_argument("site", help="Insights site, such as insights.example.com")
	parser.add_argument(
		"--ref", default="develop", help="Atlas branch, tag, or commit to take the scripts from"
	)
	parser.add_argument("--default-host", default="", help="host title that Atlas - Host opens with")
	parser.add_argument(
		"--bench",
		type=Path,
		help="bench folder that holds the site, such as ~/frappe-bench; default: find it with pilot",
	)
	parser.add_argument(
		"--source", type=Path, help="use this scripts/insights folder instead of downloading one"
	)
	parser.add_argument(
		"--alert-webhook-url",
		help="send the alerts to this https URL, such as a Raven incoming webhook",
	)
	arguments = parser.parse_args()

	bench = arguments.bench.expanduser().resolve() if arguments.bench else find_bench(arguments.site)
	if not (bench / "sites" / arguments.site / "site_config.json").exists():
		sys.exit(f"error: bench {bench} has no site {arguments.site}")
	with tempfile.TemporaryDirectory() as temporary:
		directory = Path(temporary)
		print(f"==> get the scripts from {arguments.source or f'{REPOSITORY}@{arguments.ref}'}", flush=True)
		scripts = (
			arguments.source.resolve() if arguments.source else download_scripts(arguments.ref, directory)
		)

		print("==> generate the workbook", flush=True)
		workbook = directory / "atlas-workbook.json"
		environment = {
			**os.environ,
			"ATLAS_INSIGHTS_DEFAULT_HOST": arguments.default_host,
			"ATLAS_INSIGHTS_ALERT_WEBHOOK_URL": arguments.alert_webhook_url or "",
			"PYTHONDONTWRITEBYTECODE": "1",
		}
		with workbook.open("w") as output:
			subprocess.run(
				[sys.executable, "generate_workbook.py"],
				cwd=scripts,
				stdout=output,
				env=environment,
				check=True,
			)

		print(f"==> update the workbook on {arguments.site} in bench {bench.name}", flush=True)
		command = [bench / "env" / "bin" / "python", scripts / "import_workbook.py", arguments.site, workbook]
		result = subprocess.run(command, cwd=bench / "sites", env=environment)

	sys.exit(result.returncode)


if __name__ == "__main__":
	main()
