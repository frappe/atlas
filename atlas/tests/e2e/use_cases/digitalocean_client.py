"""Use case: talk to DigitalOcean.

Exercises every code path on the DO HTTP client that an operator can trigger:

- `account()` smoke (token works).
- Real-droplet round trip: create -> poll for active -> get -> delete.
- Error paths: 404 on get_droplet, silent 404 on delete_droplet.
- Pure helpers `public_ipv4` / `public_ipv6` / `_network_cidr` covering
  every "no public address" branch.

Cost: one throwaway droplet (for the round trip). The error / helper paths
do not provision anything.
"""

import time
import traceback

from atlas.atlas.digitalocean import (
	DigitalOceanError,
	_network_cidr,
	public_ipv4,
	public_ipv6,
)
from atlas.tests.e2e._shared import (
	cleanup_droplet,
	create_test_droplet,
	get_client,
	sweep_old_droplets,
)


def run() -> None:
	"""Entry point. Runs the no-droplet checks first so a transient DO outage
	fails fast before we burn a billable droplet on the round trip."""
	start_clock = time.monotonic()
	try:
		_check_account()
		_check_get_droplet_bogus()
		_check_delete_droplet_bogus_is_silent()
		_check_public_ipv4_missing()
		_check_public_ipv6_missing()
		_check_network_cidr_helper()
		_check_real_droplet_round_trip()
	except Exception:
		elapsed = time.monotonic() - start_clock
		print(f"digitalocean-client: FAIL in {elapsed:.0f}s")
		traceback.print_exc()
		raise
	elapsed = time.monotonic() - start_clock
	print(f"digitalocean-client: OK in {elapsed:.0f}s")


def run_smoke() -> None:
	"""Host/API-only path for development. Smoke the token, then the real
	droplet create → poll active → get → delete round trip — the live-API
	facts only DigitalOcean can confirm.

	Skips the get/delete 404 paths and the pure `public_ipv4` / `public_ipv6` /
	`_network_cidr` helpers (covered by `test_digitalocean.py`)."""
	start_clock = time.monotonic()
	try:
		_check_account()
		_check_real_droplet_round_trip()
	except Exception:
		elapsed = time.monotonic() - start_clock
		print(f"digitalocean-client (smoke): FAIL in {elapsed:.0f}s")
		traceback.print_exc()
		raise
	elapsed = time.monotonic() - start_clock
	print(f"digitalocean-client (smoke): OK in {elapsed:.0f}s")


def _check_account() -> None:
	"""account() returns a dict or 403s if the token lacks `account:read`.
	Either outcome exercises the same code path."""
	client = get_client()
	try:
		account = client.account()
		assert "email" in account or "uuid" in account, account
	except DigitalOceanError as exception:
		assert "403" in str(exception) or "forbidden" in str(exception).lower(), str(exception)


def _check_get_droplet_bogus() -> None:
	client = get_client()
	caught = False
	try:
		client.get_droplet(1)
	except DigitalOceanError:
		caught = True
	assert caught, "get_droplet(1) should have raised DigitalOceanError"


def _check_delete_droplet_bogus_is_silent() -> None:
	"""allow_404 path: deleting a non-existent id returns silently."""
	client = get_client()
	client.delete_droplet(1)


def _check_public_ipv4_missing() -> None:
	caught = False
	try:
		public_ipv4({"id": 1, "networks": {"v4": []}})
	except DigitalOceanError:
		caught = True
	assert caught, "public_ipv4 with no v4 should have raised"

	caught = False
	try:
		public_ipv4({"id": 2, "networks": {"v4": [{"type": "private", "ip_address": "10.0.0.1"}]}})
	except DigitalOceanError:
		caught = True
	assert caught, "public_ipv4 with only private v4 should have raised"


def _check_public_ipv6_missing() -> None:
	caught = False
	try:
		public_ipv6({"id": 1, "networks": {"v6": []}})
	except DigitalOceanError:
		caught = True
	assert caught, "public_ipv6 with no v6 should have raised"


def _check_network_cidr_helper() -> None:
	cidr = _network_cidr("2604:a880:cad:d0::1", 64)
	assert cidr.endswith("/64"), cidr
	assert cidr.startswith("2604:a880:cad:d0:"), cidr


def _check_real_droplet_round_trip() -> None:
	"""Create -> verify active -> fetch -> delete -> assert gone."""
	client = get_client()
	sweep_old_droplets(client)

	droplet = None
	try:
		droplet = create_test_droplet(client, "do-client")
		assert droplet["status"] == "active"

		host_v4 = public_ipv4(droplet)
		host_v6, cidr_v6 = public_ipv6(droplet)
		print(f"created droplet {droplet['id']} v4={host_v4} v6={host_v6} prefix={cidr_v6}")

		fetched = client.get_droplet(droplet["id"])
		assert fetched["status"] == "active"

		client.delete_droplet(droplet["id"])
		_assert_gone(client, droplet["id"])
		droplet = None
	finally:
		if droplet:
			cleanup_droplet(client, droplet["id"])


def _assert_gone(client, droplet_id: int) -> None:
	deadline = time.monotonic() + 60
	while time.monotonic() < deadline:
		try:
			droplet = client.get_droplet(droplet_id)
		except Exception:
			return
		if droplet.get("status") in (None, "off", "archive"):
			return
		time.sleep(2)
	raise AssertionError(f"droplet {droplet_id} still present after 60s")
