import io
import runpy
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

HOOK = Path(__file__).parents[1] / "scripts" / "guest" / "rescue-reboot"


class TestGuestRescueHook(TestCase):
	def test_notification_waits_for_complete_acknowledgement(self):
		hook = runpy.run_path(str(HOOK))
		connection = Mock()
		connection.recv.side_effect = [b"o", b"k\n"]
		context = Mock()
		context.__enter__ = Mock(return_value=connection)
		context.__exit__ = Mock(return_value=False)
		with patch("socket.AF_VSOCK", 40, create=True), patch("socket.socket", return_value=context):
			hook["notify_reboot"]()
		connection.connect.assert_called_once_with((2, 187))
		connection.settimeout.assert_called_once_with(2)
		connection.sendall.assert_called_once_with(b"reboot\n")
		self.assertEqual(connection.recv.call_count, 2)

	def test_incomplete_or_invalid_acknowledgement_is_rejected(self):
		hook = runpy.run_path(str(HOOK))
		for chunks in ([b""], [b"o", b""], [b"no\n"]):
			with self.subTest(chunks=chunks):
				connection = Mock()
				connection.recv.side_effect = chunks
				context = Mock()
				context.__enter__ = Mock(return_value=connection)
				context.__exit__ = Mock(return_value=False)
				with (
					patch("socket.AF_VSOCK", 40, create=True),
					patch("socket.socket", return_value=context),
					self.assertRaisesRegex(OSError, "did not acknowledge"),
				):
					hook["notify_reboot"]()
				context.__exit__.assert_called_once()

	def test_failed_notification_reports_error_without_interrupting_shutdown(self):
		for operation, failure in (
			("recv", TimeoutError("timed out")),
			("connect", ConnectionRefusedError("refused")),
		):
			with self.subTest(operation=operation):
				connection = Mock()
				getattr(connection, operation).side_effect = failure
				context = MagicMock()
				context.__enter__.return_value = connection
				with (
					patch("sys.argv", [str(HOOK), "reboot"]),
					patch("socket.AF_VSOCK", 40, create=True),
					patch("socket.socket", return_value=context),
					patch("sys.stderr", new_callable=io.StringIO) as stderr,
				):
					runpy.run_path(str(HOOK), run_name="__main__")
				self.assertIn(f"Atlas rescue reboot notification failed: {failure}", stderr.getvalue())
				context.__exit__.assert_called_once()

	def test_other_shutdown_actions_do_not_signal_rescue_exit(self):
		for arguments in ([], ["poweroff"], ["halt"], ["kexec"]):
			with self.subTest(arguments=arguments):
				with patch("sys.argv", [str(HOOK), *arguments]), patch("socket.socket") as connect:
					runpy.run_path(str(HOOK), run_name="__main__")
				connect.assert_not_called()
