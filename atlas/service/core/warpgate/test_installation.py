from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.service.core.warpgate.client import WarpgateError
from atlas.service.core.warpgate.installation import WarpgateTokenManager

INSTALLATION_MODULE = "atlas.service.core.warpgate.installation"


def expiry_in(days: int) -> str:
	return (datetime.now(UTC) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


class TestWarpgateTokenManager(UnitTestCase):
	def run_manager(self, client: Mock, action: str, token_id: str = "held") -> SimpleNamespace:
		settings = SimpleNamespace(warpgate_api_token_id=token_id, save=Mock())
		with (
			patch(f"{INSTALLATION_MODULE}.WarpgateClient.from_settings", return_value=client),
			patch(f"{INSTALLATION_MODULE}.frappe.db"),
		):
			getattr(WarpgateTokenManager(settings), action)()
		return settings

	def test_a_token_far_from_expiry_stays(self) -> None:
		client = Mock()
		client.list_api_tokens.return_value = [{"id": "held", "expiry": expiry_in(200)}]

		settings = self.run_manager(client, "renew")

		client.create_api_token.assert_not_called()
		client.delete_api_token.assert_not_called()
		settings.save.assert_not_called()

	def test_a_token_near_expiry_is_replaced_and_the_old_one_deleted(self) -> None:
		client = Mock()
		client.list_api_tokens.side_effect = [
			[{"id": "held", "expiry": expiry_in(10)}],
			[{"id": "held", "expiry": expiry_in(10)}, {"id": "new", "expiry": expiry_in(365)}],
		]
		client.create_api_token.return_value = {"secret": "new-secret", "token": {"id": "new"}}

		settings = self.run_manager(client, "renew")

		self.assertEqual((settings.warpgate_api_token, settings.warpgate_api_token_id), ("new-secret", "new"))
		client.delete_api_token.assert_called_once_with("held")

	def test_a_renewal_that_was_not_stored_renews_the_held_token_again(self) -> None:
		"""A newer token whose secret Atlas never stored must not stop the renewal, and is deleted."""
		client = Mock()
		client.list_api_tokens.side_effect = [
			[{"id": "held", "expiry": expiry_in(10)}, {"id": "lost", "expiry": expiry_in(360)}],
			[
				{"id": "held", "expiry": expiry_in(10)},
				{"id": "lost", "expiry": expiry_in(360)},
				{"id": "new", "expiry": expiry_in(365)},
			],
		]
		client.create_api_token.return_value = {"secret": "new-secret", "token": {"id": "new"}}

		settings = self.run_manager(client, "renew")

		self.assertEqual(settings.warpgate_api_token_id, "new")
		self.assertEqual(
			sorted(call.args[0] for call in client.delete_api_token.call_args_list), ["held", "lost"]
		)

	def test_cleanup_keeps_the_stored_token_not_the_newest(self) -> None:
		client = Mock()
		client.list_api_tokens.return_value = [
			{"id": "held", "expiry": expiry_in(300)},
			{"id": "lost", "expiry": expiry_in(365)},
		]

		self.run_manager(client, "delete_other_tokens")

		client.delete_api_token.assert_called_once_with("lost")

	def test_cleanup_without_a_stored_token_id_deletes_nothing(self) -> None:
		client = Mock()

		with self.assertRaisesRegex(WarpgateError, "API Token ID"):
			self.run_manager(client, "delete_other_tokens", token_id="")
		client.delete_api_token.assert_not_called()

	def test_an_expired_token_asks_for_a_setup_run(self) -> None:
		client = Mock()
		client.list_api_tokens.side_effect = WarpgateError("returned 401")

		with self.assertRaisesRegex(WarpgateError, "Run atlas-vm setup again"):
			self.run_manager(client, "renew")

	def test_an_unknown_token_id_asks_for_a_setup_run(self) -> None:
		client = Mock()
		client.list_api_tokens.return_value = [{"id": "other", "expiry": expiry_in(200)}]

		with self.assertRaisesRegex(WarpgateError, "Run atlas-vm setup again"):
			self.run_manager(client, "renew", token_id="")
		client.create_api_token.assert_not_called()
