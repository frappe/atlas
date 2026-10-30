from __future__ import annotations

from frappe.tests import UnitTestCase

from atlas.atlas.core.scrub import scrub, scrub_complete_lines, scrub_value


class TestScrub(UnitTestCase):
	def test_masks_credentials(self) -> None:
		cases = {
			"password=hunter2 next": "password=[REDACTED] next",
			'"api_key": "abc123"': '"api_key": "[REDACTED]"',
			"PrivateKey = yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk=": "PrivateKey = [REDACTED]",
			"Authorization: Bearer abcdefgh.ijkl": "Authorization: Bearer [REDACTED]",
			"url https://user:pa55@host.example/x": "url https://[REDACTED]@host.example/x",
			"get /f?X-Amz-Signature=deadbeef&a=1": "get /f?X-Amz-Signature=[REDACTED]&a=1",
			"key sk-or-v1-0123456789abcdef0123": "key [REDACTED:key]",
			"jwt eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlLXZhbHVl": "jwt [REDACTED:jwt]",
			"aws AKIAIOSFODNN7EXAMPLE": "aws [REDACTED:key]",
		}
		for text, expected in cases.items():
			with self.subTest(text=text):
				self.assertEqual(scrub(text), expected)

	def test_masks_random_words_and_hashes(self) -> None:
		self.assertEqual(scrub("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"), "[REDACTED:entropy]")
		self.assertEqual(scrub("sha 9f86d081884c7d659a2feaa0c55ad015"), "sha [REDACTED:entropy]")

	def test_keeps_what_an_investigation_needs(self) -> None:
		text = (
			"vm-0000076 on 01a0cb6d-7f90-766a-8d2c-2f768d762045 fdab:1:7f90:766a:8d2c:2f76:8d76:2045 "
			"10.0.0.4 metal-vm@vm-0000076.service /var/lib/metal/machines/vm-0000076/firecracker "
			"alice@example.com waw-1 ExceptionGroupInternalServerError"
		)
		self.assertEqual(scrub(text), text)

	def test_scrubs_each_line_alone(self) -> None:
		text = "token=one\nok line\npassword=two\n"
		self.assertEqual(scrub(text), "".join(scrub(line) for line in text.splitlines(keepends=True)))

	def test_complete_lines_hold_back_a_partial_secret(self) -> None:
		self.assertEqual(scrub_complete_lines("done\npassword=hun"), "done\n")

	def test_secret_like_keys_hide_their_values(self) -> None:
		value = {"db_password": "plain", "rows": [{"token": 5, "host": "waw-1"}], "secret": ""}
		self.assertEqual(
			scrub_value(value),
			{"db_password": "[REDACTED]", "rows": [{"token": "[REDACTED]", "host": "waw-1"}], "secret": ""},
		)
