from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.atlas.core.exceptions import AtlasConflictError, AtlasUserError
from atlas.service.core.warpgate.access import HostAccess, HostNotFound, WarpgateUnavailable, close_sessions
from atlas.service.core.warpgate.client import WarpgateError

ACCESS_MODULE = "atlas.service.core.warpgate.access"


class FakeWarpgate:
	"""Keep users, SSO credentials, and role grants in memory, like the Warpgate admin API."""

	def __init__(self) -> None:
		self.roles = {"all-hosts": "role-all", "host:osa-2": "role-osa-2"}
		self.users: dict[str, dict] = {}
		self.credentials: list[tuple[str, str, str]] = []
		self.grants: dict[tuple[str, str], datetime] = {}
		self.sessions: dict[str, str] = {}
		self.calls: list[str] = []

	def list_roles(self) -> list[dict]:
		return [{"name": name, "id": role_id} for name, role_id in self.roles.items()]

	def find_user(self, username: str) -> dict | None:
		return self.users.get(username)

	def create_user(self, username: str) -> dict:
		self.users[username] = {"id": f"user-{username}", "username": username, "credential_policy": None}
		return self.users[username]

	def get_user(self, user_id: str) -> dict:
		return next(user for user in self.users.values() if user["id"] == user_id)

	def set_credential_policy(self, user: dict, credential_policy: dict) -> None:
		self.users[user["username"]]["credential_policy"] = {**credential_policy, "rdp": None}

	def list_sso_credentials(self, user_id: str) -> list[dict]:
		return [
			{"provider": provider, "email": email}
			for owner, provider, email in self.credentials
			if owner == user_id
		]

	def add_sso_credential(self, user_id: str, provider: str, email: str) -> None:
		self.credentials.append((user_id, provider, email))

	def list_user_roles(self, user_id: str) -> list[dict]:
		return [{"id": role_id} for (owner, role_id) in self.grants if owner == user_id]

	def grant_user_role(self, user_id: str, role_id: str, expires_at: datetime, *, is_granted: bool) -> None:
		self.calls.append("PUT" if is_granted else "POST")
		self.grants[(user_id, role_id)] = expires_at

	def revoke_user_role(self, user_id: str, role_id: str) -> None:
		self.calls.append("DELETE")
		del self.grants[(user_id, role_id)]

	def list_active_sessions(self, username: str) -> list[dict]:
		return [{"id": session_id} for session_id, owner in self.sessions.items() if owner == username]

	def close_session(self, session_id: str) -> None:
		del self.sessions[session_id]


def in_hours(hours: float) -> datetime:
	return datetime.now(UTC) + timedelta(hours=hours)


class TestHostAccess(UnitTestCase):
	def access(self, warpgate: FakeWarpgate, host_id: str = "server-2") -> HostAccess:
		with patch(f"{ACCESS_MODULE}.frappe.db.get_value", return_value="osa-2"):
			return HostAccess(host_id, warpgate)

	def test_a_grant_registers_the_person_for_central_sign_in(self) -> None:
		"""Central can grant before the person ever signs in to Warpgate."""
		warpgate = FakeWarpgate()

		self.access(warpgate).grant(" Alice@Frappe.io ", in_hours(2))

		self.assertEqual(warpgate.credentials, [("user-alice@frappe.io", "central", "alice@frappe.io")])
		self.assertEqual(warpgate.users["alice@frappe.io"]["credential_policy"]["ssh"], ["WebUserApproval"])
		self.assertIn(("user-alice@frappe.io", "role-osa-2"), warpgate.grants)

	def test_a_second_grant_moves_the_end_time(self) -> None:
		warpgate = FakeWarpgate()
		access = self.access(warpgate)
		access.grant("alice@frappe.io", in_hours(1))

		later = in_hours(5)
		access.grant("alice@frappe.io", later)

		self.assertEqual(warpgate.calls, ["POST", "PUT"])
		self.assertEqual(warpgate.grants[("user-alice@frappe.io", "role-osa-2")], later)
		self.assertEqual(len(warpgate.credentials), 1)

	def test_all_grants_the_all_hosts_role(self) -> None:
		warpgate = FakeWarpgate()

		HostAccess("all", warpgate).grant("bob@frappe.io", in_hours(8))

		self.assertIn(("user-bob@frappe.io", "role-all"), warpgate.grants)

	def test_revoking_twice_or_for_an_unknown_person_is_safe(self) -> None:
		warpgate = FakeWarpgate()
		access = self.access(warpgate)
		access.grant("alice@frappe.io", in_hours(1))

		access.revoke("alice@frappe.io")
		access.revoke("alice@frappe.io")
		access.revoke("nobody@frappe.io")

		self.assertEqual(warpgate.grants, {})
		self.assertEqual(warpgate.calls, ["POST", "DELETE"])

	def test_a_revoke_closes_every_live_session_of_the_person(self) -> None:
		warpgate = FakeWarpgate()
		access = self.access(warpgate)
		access.grant("alice@frappe.io", in_hours(1))
		warpgate.sessions = {"s1": "alice@frappe.io", "s2": "alice@frappe.io", "s3": "bob@frappe.io"}

		access.revoke("alice@frappe.io")

		self.assertEqual(warpgate.sessions, {"s3": "bob@frappe.io"})

	def test_an_expiry_must_have_a_time_zone_be_future_and_within_the_cap(self) -> None:
		access = self.access(FakeWarpgate())

		for expires_at in (datetime.now() + timedelta(hours=1), in_hours(-1), in_hours(25)):
			with self.assertRaises(AtlasUserError):
				access.grant("alice@frappe.io", expires_at)

		with patch(f"{ACCESS_MODULE}.frappe.conf", {"warpgate_grant_max_hours": 48}):
			access.grant("alice@frappe.io", in_hours(30))

	def test_an_email_must_be_one_plain_address(self) -> None:
		self.assertEqual(HostAccess.normalize_email(" Alice@Example.com "), "alice@example.com")
		for email in ("Alice <alice@example.com>", "alice@example.com, bob@example.com", "alice"):
			with self.assertRaises(AtlasUserError):
				HostAccess.normalize_email(email)

	def test_an_unknown_host_is_not_found(self) -> None:
		with (
			patch(f"{ACCESS_MODULE}.frappe.db.get_value", return_value=None),
			self.assertRaises(HostNotFound),
		):
			HostAccess("missing", FakeWarpgate())

	def test_a_host_that_the_sync_has_not_added_yet_is_a_conflict(self) -> None:
		warpgate = FakeWarpgate()
		del warpgate.roles["host:osa-2"]

		with self.assertRaises(AtlasConflictError):
			self.access(warpgate).grant("alice@frappe.io", in_hours(1))

	def test_a_warpgate_failure_is_a_retryable_unavailable_error(self) -> None:
		warpgate = FakeWarpgate()
		warpgate.list_roles = Mock(side_effect=WarpgateError("Warpgate is unreachable"))

		with self.assertRaises(WarpgateUnavailable):
			self.access(warpgate).grant("alice@frappe.io", in_hours(1))

	def test_closing_sessions_keeps_the_roles_and_other_people(self) -> None:
		warpgate = FakeWarpgate()
		self.access(warpgate).grant("alice@frappe.io", in_hours(1))
		warpgate.sessions = {"s1": "alice@frappe.io", "s2": "bob@frappe.io"}

		close_sessions("Alice@frappe.io", warpgate)

		self.assertEqual(warpgate.sessions, {"s2": "bob@frappe.io"})
		self.assertEqual(len(warpgate.grants), 1)

	def test_closing_sessions_without_warpgate_does_nothing(self) -> None:
		with patch(f"{ACCESS_MODULE}.WarpgateClient.from_settings", return_value=None):
			close_sessions("alice@frappe.io")

	def test_a_region_without_warpgate_is_unavailable(self) -> None:
		with (
			patch(f"{ACCESS_MODULE}.WarpgateClient.from_settings", return_value=None),
			self.assertRaises(WarpgateUnavailable),
		):
			HostAccess("all")

	def test_a_grant_repairs_a_registration_that_stopped_halfway(self) -> None:
		warpgate = FakeWarpgate()
		warpgate.create_user("alice@frappe.io")

		self.access(warpgate).grant("alice@frappe.io", in_hours(1))
		self.access(warpgate).grant("alice@frappe.io", in_hours(2))

		self.assertEqual(warpgate.credentials, [("user-alice@frappe.io", "central", "alice@frappe.io")])
		self.assertEqual(warpgate.users["alice@frappe.io"]["credential_policy"]["http"], ["Sso"])
