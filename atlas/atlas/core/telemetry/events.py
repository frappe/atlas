from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import frappe
from redis.client import Pipeline
from redis.exceptions import RedisError

COUNTERS_KEY = "atlas:telemetry:counters"
OBSERVATIONS_KEY = "atlas:telemetry:observations"

# Bound memory use during a Datum outage.
MAXIMUM_OBSERVATIONS = 50_000

METRIC_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,199}$")

# Datum retains series forever. Put resource and request IDs in logs.
CHURNING_LABELS = frozenset(
	{"virtual_machine", "virtual_machine_id", "request_id", "migration", "tenant_id", "uuid"}
)


def count(metric: str, value: float = 1, **labels: str) -> None:
	"""Add value to a counter that every web and worker process shares."""
	key = _series_key(metric, labels)
	pipeline = frappe.cache.pipeline()
	pipeline.hincrbyfloat(frappe.cache.make_key(COUNTERS_KEY), key, value)
	_execute(pipeline, metric)


def observe(metric: str, value: float, **labels: str) -> None:
	"""Record one event value, such as a duration, for exact percentiles in Datum."""
	_series_key(metric, labels)
	sample = {"metric": metric, "value": value, "ts": datetime.now(UTC).isoformat(), "labels": labels}
	key = frappe.cache.make_key(OBSERVATIONS_KEY)
	pipeline = frappe.cache.pipeline()
	pipeline.rpush(key, json.dumps(sample))
	pipeline.ltrim(key, -MAXIMUM_OBSERVATIONS, -1)
	_execute(pipeline, metric)


@contextmanager
def timed(metric: str, **labels: str) -> Iterator[None]:
	"""Observe elapsed time and label the result with any exception type."""
	started_at = time.monotonic()
	result = "ok"
	try:
		yield
	except Exception as exception:
		result = _snake_case(type(exception).__name__)
		raise
	finally:
		observe(metric, time.monotonic() - started_at, **labels, result=result)


def read_counters(timestamp: str) -> list[dict]:
	"""Return every counter as a Datum sample at timestamp."""
	pipeline = frappe.cache.pipeline()
	pipeline.hgetall(frappe.cache.make_key(COUNTERS_KEY))
	(values,) = pipeline.execute()
	samples = []
	for key, value in values.items():
		metric, labels = json.loads(key)
		samples.append({"metric": metric, "value": float(value), "ts": timestamp, "labels": labels})
	return samples


def take_observations() -> list[dict]:
	"""Remove and return the pending observations in one step."""
	key = frappe.cache.make_key(OBSERVATIONS_KEY)
	pipeline = frappe.cache.pipeline()
	pipeline.lrange(key, 0, -1)
	pipeline.delete(key)
	values, _deleted = pipeline.execute()
	return [json.loads(value) for value in values]


def restore_observations(observations: list[dict]) -> None:
	"""Put back observations that Datum did not accept, keeping the newest ones."""
	if not observations:
		return
	key = frappe.cache.make_key(OBSERVATIONS_KEY)
	pipeline = frappe.cache.pipeline()
	pipeline.lpush(key, *[json.dumps(sample) for sample in reversed(observations)])
	pipeline.ltrim(key, -MAXIMUM_OBSERVATIONS, -1)
	pipeline.execute()


def _execute(pipeline: Pipeline, metric: str) -> None:
	"""Use raw values because Frappe pickles hashes; drop samples on cache errors."""
	try:
		pipeline.execute()
	except RedisError as error:
		frappe.logger("atlas.telemetry").warning(f"Dropped {metric}: {error}")


def _series_key(metric: str, labels: dict[str, str]) -> str:
	"""Return a stable key for one series, and reject a name Datum would refuse."""
	if not METRIC_NAME.match(metric):
		raise ValueError(f"Invalid metric name {metric!r}")
	if churning := CHURNING_LABELS.intersection(labels):
		raise ValueError(f"Metric {metric} uses labels that add a series per resource: {sorted(churning)}")
	return json.dumps([metric, labels], sort_keys=True)


def _snake_case(name: str) -> str:
	return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
