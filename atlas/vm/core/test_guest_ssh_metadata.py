import io
import runpy
import syslog
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

SCRIPT = Path(__file__).parents[1] / "scripts/guest/authorized-keys-command"
SECRET = "sensitive-token-key-or-response\nforged-log-entry"


class TestGuestSSHMetadata(TestCase):
	def _run_helper(self, responses):
		with (
			patch("urllib.request.urlopen", side_effect=responses) as request,
			patch("syslog.syslog") as logger,
			patch("sys.stdout", new_callable=io.StringIO) as output,
			patch("sys.stderr", new_callable=io.StringIO) as stderr,
		):
			runpy.run_path(str(SCRIPT))
		return request, logger, output.getvalue(), stderr.getvalue()

	def test_success_preserves_keys_and_does_not_log(self):
		responses = [
			Mock(read=Mock(return_value=value)) for value in (b"token", b"0\n1", b"key-one", b"key-two")
		]
		request, logger, output, stderr = self._run_helper(responses)
		self.assertEqual(output, "key-one\nkey-two\n")
		self.assertEqual(stderr, "")
		logger.assert_not_called()
		self.assertTrue(all(call.kwargs == {"timeout": 3} for call in request.call_args_list))
		self.assertEqual(request.call_args_list[0].args[0].method, "PUT")
		self.assertEqual(request.call_args_list[-1].args[0].get_header("X-metadata-token"), "token")

	def test_empty_key_list_is_silent(self):
		_, logger, output, stderr = self._run_helper([io.BytesIO(b"token"), io.BytesIO(b"")])
		self.assertEqual((output, stderr), ("", ""))
		logger.assert_not_called()

	def test_failures_log_stage_and_category_without_sensitive_content(self):
		for stage, preceding in (
			("token", []),
			("key-list", [SECRET.encode()]),
			("key", [SECRET.encode(), b"0"]),
		):
			for failure, category in (
				(TimeoutError(SECRET), "TimeoutError"),
				(HTTPError(SECRET, 401, SECRET, {}, io.BytesIO(SECRET.encode())), "HTTPError status=401"),
				(URLError(TimeoutError(SECRET)), "URLError reason=TimeoutError"),
				(URLError(SECRET), "URLError"),
				(OSError(SECRET), "OSError"),
				(ValueError(SECRET), "ValueError"),
			):
				if isinstance(failure, HTTPError):
					self.addCleanup(failure.close)
				with self.subTest(stage=stage, category=category):
					responses = [io.BytesIO(value) for value in preceding] + [failure]
					request, logger, output, stderr = self._run_helper(responses)
					self.assertEqual((output, stderr), ("", ""))
					logger.assert_called_once_with(
						syslog.LOG_AUTHPRIV | syslog.LOG_WARNING,
						f"atlas-ssh-metadata: stage={stage} error={category}",
					)
					self.assertEqual(request.call_count, len(preceding) + 1)

	def test_read_and_decode_failures_are_logged(self):
		for response, category in (
			(Mock(read=Mock(side_effect=TimeoutError(SECRET))), "TimeoutError"),
			(io.BytesIO(SECRET.encode() + b"\xff"), "UnicodeDecodeError"),
		):
			with self.subTest(category=category):
				_, logger, output, _ = self._run_helper([io.BytesIO(b"token"), io.BytesIO(b"0"), response])
				self.assertEqual(output, "")
				logger.assert_called_once_with(
					syslog.LOG_AUTHPRIV | syslog.LOG_WARNING,
					f"atlas-ssh-metadata: stage=key error={category}",
				)

	def test_later_key_failure_preserves_prior_output_without_logging_key_or_index(self):
		responses = [
			io.BytesIO(b"token"),
			io.BytesIO(b"0\n" + SECRET.encode()),
			io.BytesIO(b"accepted-key"),
			HTTPError(SECRET, 404, SECRET, {}, None),
		]
		self.addCleanup(responses[-1].close)
		_, logger, output, _ = self._run_helper(responses)
		self.assertEqual(output, "accepted-key\n")
		logger.assert_called_once_with(
			syslog.LOG_AUTHPRIV | syslog.LOG_WARNING,
			"atlas-ssh-metadata: stage=key error=HTTPError status=404",
		)
