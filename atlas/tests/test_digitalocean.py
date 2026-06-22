import json
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.digitalocean import (
	DigitalOceanClient,
	DigitalOceanError,
	public_ipv4,
	public_ipv6,
	reserved_ip_droplet_id,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "digitalocean"


def _fixture(name: str) -> dict:
	return json.loads((FIXTURE_DIR / f"{name}.json").read_text())


class _FakeResponse:
	def __init__(self, status_code: int, body: dict | None = None, headers: dict | None = None):
		self.status_code = status_code
		self._body = body or {}
		self.text = json.dumps(self._body) if body is not None else ""
		self.content = self.text.encode() if self.text else b""
		self.headers = headers or {}

	def json(self):
		return self._body


class TestDigitalOceanClient(IntegrationTestCase):
	def setUp(self) -> None:
		self.client = DigitalOceanClient(token="dop_v1_test")

	def test_account_ok(self) -> None:
		fake = _FakeResponse(200, _fixture("account"))
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake) as request:
			account = self.client.account()
		self.assertEqual(account["email"], "test@example.com")
		_, kwargs = request.call_args
		self.assertEqual(kwargs["headers"]["Authorization"], "Bearer dop_v1_test")

	def test_account_bad_token(self) -> None:
		fake = _FakeResponse(401, _fixture("error_unauthorized"))
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake):
			with self.assertRaises(DigitalOceanError):
				self.client.account()

	def test_create_droplet_request_shape(self) -> None:
		fake = _FakeResponse(202, _fixture("droplet_new"))
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake) as request:
			self.client.create_droplet(
				name="atlas-e2e-x",
				region="blr1",
				size="s-2vcpu-4gb-intel",
				image="ubuntu-24-04-x64",
				ssh_key_ids=["12:34:56"],
				tags=["atlas-e2e"],
				ipv6=True,
			)
		_, kwargs = request.call_args
		body = kwargs["json"]
		self.assertEqual(body["name"], "atlas-e2e-x")
		self.assertEqual(body["region"], "blr1")
		self.assertEqual(body["size"], "s-2vcpu-4gb-intel")
		self.assertEqual(body["image"], "ubuntu-24-04-x64")
		self.assertEqual(body["ssh_keys"], ["12:34:56"])
		self.assertEqual(body["tags"], ["atlas-e2e"])
		self.assertTrue(body["ipv6"])

	def test_delete_droplet_treats_404_as_success(self) -> None:
		fake = _FakeResponse(404)
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake):
			self.client.delete_droplet(412345678)

	def test_public_ipv6_from_droplet_fixture(self) -> None:
		droplet = _fixture("droplet_active")["droplet"]
		host, cidr = public_ipv6(droplet)
		self.assertEqual(host, "2a03:b0c0:abcd:1234::1")
		self.assertEqual(cidr, "2a03:b0c0:abcd:1234::/64")

	def test_public_ipv4_from_droplet_fixture(self) -> None:
		droplet = _fixture("droplet_active")["droplet"]
		self.assertEqual(public_ipv4(droplet), "139.59.1.2")

	def test_list_droplets_by_tag_returns_array(self) -> None:
		fake = _FakeResponse(200, {"droplets": [{"id": 1}, {"id": 2}]})
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake) as request:
			droplets = self.client.list_droplets_by_tag("atlas-e2e")
		self.assertEqual([d["id"] for d in droplets], [1, 2])
		args, _ = request.call_args
		self.assertIn("tag_name=atlas-e2e", args[1])

	def test_list_droplets_by_tag_handles_missing_droplets_key(self) -> None:
		fake = _FakeResponse(200, {})
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake):
			droplets = self.client.list_droplets_by_tag("nonexistent")
		self.assertEqual(droplets, [])

	def test_list_droplets_unfiltered_returns_array(self) -> None:
		fake = _FakeResponse(200, {"droplets": [{"id": 1}, {"id": 2}, {"id": 3}]})
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake) as request:
			droplets = self.client.list_droplets()
		self.assertEqual([d["id"] for d in droplets], [1, 2, 3])
		args, _ = request.call_args
		# Unfiltered: no tag_name; a per_page so the account's handful comes back.
		self.assertNotIn("tag_name", args[1])
		self.assertIn("per_page=200", args[1])

	def test_list_droplets_handles_missing_droplets_key(self) -> None:
		fake = _FakeResponse(200, {})
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake):
			droplets = self.client.list_droplets()
		self.assertEqual(droplets, [])

	def test_request_handles_204_no_content(self) -> None:
		fake = _FakeResponse(204)
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake):
			result = self.client._request("DELETE", "/droplets/1")
		self.assertEqual(result, {})

	def test_request_handles_empty_200_body(self) -> None:
		class EmptyResponse:
			status_code = 200
			text = ""
			content = b""

			def json(self):
				return {}

		with patch("atlas.atlas.digitalocean.requests.request", return_value=EmptyResponse()):
			result = self.client._request("GET", "/something")
		self.assertEqual(result, {})

	def test_public_ipv6_raises_without_public_entry(self) -> None:
		droplet = {
			"id": 1,
			"networks": {"v6": [{"type": "private", "ip_address": "fd00::1"}]},
		}
		with self.assertRaises(DigitalOceanError):
			public_ipv6(droplet)

	def test_public_ipv6_raises_when_v6_missing(self) -> None:
		droplet = {"id": 2, "networks": {}}
		with self.assertRaises(DigitalOceanError):
			public_ipv6(droplet)

	def test_public_ipv4_raises_without_public_entry(self) -> None:
		droplet = {
			"id": 3,
			"networks": {"v4": [{"type": "private", "ip_address": "10.0.0.1"}]},
		}
		with self.assertRaises(DigitalOceanError):
			public_ipv4(droplet)

	def test_verify_credentials_extracts_rate_limit_headers(self) -> None:
		fake = _FakeResponse(
			200,
			_fixture("account"),
			headers={"RateLimit-Limit": "5000", "RateLimit-Remaining": "4998"},
		)
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake):
			result = self.client.verify_credentials()
		self.assertEqual(result["email"], "test@example.com")
		self.assertEqual(result["rate_limit"], 5000)
		self.assertEqual(result["rate_remaining"], 4998)

	def test_verify_credentials_handles_missing_headers(self) -> None:
		fake = _FakeResponse(200, _fixture("account"), headers={})
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake):
			result = self.client.verify_credentials()
		self.assertIsNone(result["rate_limit"])
		self.assertIsNone(result["rate_remaining"])

	def test_verify_credentials_raises_on_bad_token(self) -> None:
		fake = _FakeResponse(401, _fixture("error_unauthorized"))
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake):
			with self.assertRaises(DigitalOceanError):
				self.client.verify_credentials()

	# --- Reserved IPs ----------------------------------------------------

	def test_create_reserved_ip_posts_region(self) -> None:
		fake = _FakeResponse(
			202, {"reserved_ip": {"ip": "203.0.113.5", "region": {"slug": "blr1"}, "droplet": None}}
		)
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake) as request:
			reserved = self.client.create_reserved_ip("blr1")
		self.assertEqual(reserved["ip"], "203.0.113.5")
		args, kwargs = request.call_args
		self.assertEqual(args[0], "POST")
		self.assertTrue(args[1].endswith("/reserved_ips"))
		self.assertEqual(kwargs["json"], {"region": "blr1"})

	def test_assign_reserved_ip_posts_action(self) -> None:
		fake = _FakeResponse(201, {"action": {"id": 1, "type": "assign_ip", "status": "in-progress"}})
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake) as request:
			action = self.client.assign_reserved_ip("203.0.113.5", 999)
		self.assertEqual(action["type"], "assign_ip")
		args, kwargs = request.call_args
		self.assertTrue(args[1].endswith("/reserved_ips/203.0.113.5/actions"))
		self.assertEqual(kwargs["json"], {"type": "assign", "droplet_id": 999})

	def test_unassign_reserved_ip_posts_action_then_waits_for_settle(self) -> None:
		# unassign POSTs the action, then POLLS the IP until `droplet` is null
		# (DO's unassign is async; delete/re-assign before it settles -> 422). Here
		# the first GET already shows it unassigned, so the poll exits at once.
		action = _FakeResponse(201, {"action": {"id": 2, "type": "unassign_ip", "status": "in-progress"}})
		settled = _FakeResponse(200, {"reserved_ip": {"ip": "203.0.113.5", "droplet": None}})
		with patch("atlas.atlas.digitalocean.requests.request", side_effect=[action, settled]) as request:
			self.client.unassign_reserved_ip("203.0.113.5")
		# First call is the unassign POST; second is the settle-poll GET.
		first_args, first_kwargs = request.call_args_list[0]
		self.assertEqual(first_kwargs["json"], {"type": "unassign"})
		self.assertTrue(first_args[1].endswith("/reserved_ips/203.0.113.5/actions"))
		second_args, _ = request.call_args_list[1]
		self.assertEqual(second_args[0], "GET")
		self.assertTrue(second_args[1].endswith("/reserved_ips/203.0.113.5"))

	def test_unassign_settle_tolerates_404(self) -> None:
		# If the IP is already gone by the time we poll, that's settled too.
		action = _FakeResponse(201, {"action": {"id": 2, "type": "unassign_ip"}})
		gone = _FakeResponse(404)
		with patch("atlas.atlas.digitalocean.requests.request", side_effect=[action, gone]):
			self.client.unassign_reserved_ip("203.0.113.5")

	def test_list_reserved_ips_returns_array(self) -> None:
		fake = _FakeResponse(200, {"reserved_ips": [{"ip": "203.0.113.5"}, {"ip": "203.0.113.6"}]})
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake):
			reserved = self.client.list_reserved_ips()
		self.assertEqual([r["ip"] for r in reserved], ["203.0.113.5", "203.0.113.6"])

	def test_list_reserved_ips_handles_missing_key(self) -> None:
		fake = _FakeResponse(200, {})
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake):
			self.assertEqual(self.client.list_reserved_ips(), [])

	def test_delete_reserved_ip_treats_404_as_success(self) -> None:
		fake = _FakeResponse(404)
		with patch("atlas.atlas.digitalocean.requests.request", return_value=fake):
			self.client.delete_reserved_ip("203.0.113.5")

	def test_reserved_ip_droplet_id_reads_embedded_droplet(self) -> None:
		self.assertEqual(reserved_ip_droplet_id({"ip": "203.0.113.5", "droplet": {"id": 999}}), 999)

	def test_reserved_ip_droplet_id_none_when_floating(self) -> None:
		self.assertIsNone(reserved_ip_droplet_id({"ip": "203.0.113.5", "droplet": None}))
