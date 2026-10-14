from __future__ import annotations

from datetime import UTC, datetime, timedelta

import frappe
import requests

from atlas.atlas.core.telemetry.collectors import AtlasGauges
from atlas.atlas.core.telemetry.events import read_counters, restore_observations, take_observations
from atlas.auth.issuer import TOKEN_LIFETIME, issue_token

# The most samples, or log lines, Datum accepts in one request.
MAXIMUM_BATCH = 10_000
REQUEST_TIMEOUT_SECONDS = 10

# A host token outlives a few missed daily renewals. Datum has no revocation list.
HOST_TOKEN_LIFETIME = timedelta(days=7)


def get_datum_url() -> str | None:
	"""Return the regional Datum base URL from site config, or None when telemetry is off."""
	url = frappe.conf.get("datum_url")
	return url.rstrip("/") if url else None


def issue_datum_token(resource_id: str, lifetime: timedelta = TOKEN_LIFETIME) -> str:
	"""Return a write token for one machine. Datum stamps every row with its resource_id."""
	settings = frappe.get_cached_doc("Atlas Settings")
	return issue_token(
		settings,
		audience=settings.datum_audience_id,
		subject=resource_id,
		scope="datum",
		resource_id=resource_id,
		access=["write"],
		lifetime=lifetime,
	)


def get_atlas_resource_id() -> str:
	"""Name this region's Atlas in Datum. Hosts use their Metal Server name."""
	settings = frappe.get_cached_doc("Atlas Settings")
	return f"atlas-{settings.region_name}"


def publish_metrics() -> None:
	"""Push Atlas gauges, counters, and pending observations to Datum. Runs every minute."""
	url = get_datum_url()
	if not url:
		return

	token = issue_datum_token(get_atlas_resource_id())
	timestamp = datetime.now(UTC).isoformat()
	samples = AtlasGauges(timestamp).collect() + read_counters(timestamp)
	observations = take_observations()
	try:
		send_samples(url, token, samples + observations)
	except requests.RequestException as error:
		restore_observations(observations)
		frappe.logger("atlas.telemetry").warning(f"Datum push failed: {error}")


def send_samples(url: str, token: str, samples: list[dict]) -> None:
	"""Post samples in batches that Datum accepts. Raises on the first failed batch."""
	with requests.Session() as session:
		session.headers["Authorization"] = f"Bearer {token}"
		for start in range(0, len(samples), MAXIMUM_BATCH):
			response = session.post(
				f"{url}/v1/ingest",
				json={"samples": samples[start : start + MAXIMUM_BATCH]},
				timeout=REQUEST_TIMEOUT_SECONDS,
			)
			response.raise_for_status()


def send_log_lines(url: str, token: str, lines: list[dict]) -> None:
	"""Post up to MAXIMUM_BATCH log lines in one request. Raises when Datum rejects them."""
	response = requests.post(
		f"{url}/v1/logs/ingest",
		json={"lines": lines},
		headers={"Authorization": f"Bearer {token}"},
		timeout=REQUEST_TIMEOUT_SECONDS,
	)
	response.raise_for_status()
