import hashlib
import hmac
import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import satellite_events as module

URL = "http://satellite.localhost/api/method/satellite.api.webhook.receive"
SECRET = "webhook-shared-secret"


def _configure(url: str = URL, secret: str = SECRET) -> None:
	settings = frappe.get_single("Atlas Settings")
	settings.satellite_webhook_url = url
	settings.satellite_webhook_secret = secret
	settings.save(ignore_permissions=True)


class TestSatelliteEvents(IntegrationTestCase):
	def setUp(self) -> None:
		self.addCleanup(_configure, "", "")
		_configure()

	def test_after_insert_enqueues_registered_event(self) -> None:
		with patch.object(module.frappe, "enqueue") as enqueue:
			module.on_vm_after_insert(frappe._dict(name="vm-abc"))
		enqueue.assert_called_once()
		_, kwargs = enqueue.call_args
		self.assertEqual(kwargs["event"], "vm.registered")
		self.assertEqual(kwargs["vm"], "vm-abc")

	def test_no_emit_when_no_satellite_configured(self) -> None:
		_configure("", "")
		with patch.object(module.frappe, "enqueue") as enqueue:
			module.on_vm_after_insert(frappe._dict(name="vm-abc"))
		enqueue.assert_not_called()

	def test_routing_intent_change_emits_updated(self) -> None:
		# A routing-intent change (Site/Pilot recorded/dropped a subdomain) must emit even
		# without a status change, so the Satellite re-pulls and reconciles its routes.
		doc = frappe._dict(name="vm-x", status="Running", routing_subdomains='["mysite"]')
		doc.get_doc_before_save = lambda: frappe._dict(status="Running", routing_subdomains="")
		with patch.object(module.frappe, "enqueue") as enqueue:
			module.on_vm_update(doc)
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["event"], "vm.updated")

	def test_no_emit_when_nothing_meaningful_changed(self) -> None:
		doc = frappe._dict(name="vm-x", status="Running", routing_subdomains='["mysite"]')
		doc.get_doc_before_save = lambda: frappe._dict(status="Running", routing_subdomains='["mysite"]')
		with patch.object(module.frappe, "enqueue") as enqueue:
			module.on_vm_update(doc)
		enqueue.assert_not_called()

	def test_deliver_posts_hmac_signed_body(self) -> None:
		captured: dict = {}

		def fake_post(url, data, headers, timeout):
			captured.update(url=url, data=data, headers=headers)
			return frappe._dict(status_code=200)

		with patch("requests.post", side_effect=fake_post):
			module.deliver("vm.registered", "vm-abc")

		self.assertEqual(captured["url"], URL)
		body = json.loads(captured["data"])
		self.assertEqual(body["event"], "vm.registered")
		self.assertEqual(body["virtual_machine"], "vm-abc")
		expected = hmac.new(SECRET.encode(), captured["data"].encode(), hashlib.sha256).hexdigest()
		self.assertEqual(captured["headers"][module.SIGNATURE_HEADER], expected)
