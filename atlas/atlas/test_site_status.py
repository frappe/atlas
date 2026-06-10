"""Unit tests for the provisioning step view (atlas.atlas.site_status).

Pure mapping logic — `status` → the six-step checklist — plus the owner-gated
poll endpoint. Milliseconds, no host. The realtime push itself is exercised
through Site.auto_provision's tests (the status machine); here we pin the step
states and the access gate."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, get_datetime

from atlas.atlas import site_status


class TestStepsFor(IntegrationTestCase):
	def _states(self, status):
		return [s["state"] for s in site_status.steps_for(status)]

	def test_pending_has_nothing_done(self):
		# Pending = the job hasn't started; nothing is done yet.
		states = self._states("Pending")
		self.assertNotIn("done", states)
		self.assertNotIn("running", states)

	def test_provisioning_runs_both_provision_phase_steps(self):
		# Provisioning sits at the provision phase: BOTH provision-phase steps are
		# in flight (clone+provision, then the boot wait), the rest pending.
		states = self._states("Provisioning")
		self.assertEqual(states[0], "running")
		self.assertEqual(states[1], "running")
		self.assertTrue(all(s == "pending" for s in states[2:]))

	def test_deploying_marks_provision_done_deploy_running(self):
		states = self._states("Deploying")
		# The two provision steps are done, the deploy-phase steps are now running.
		self.assertEqual(states[0], "done")
		self.assertEqual(states[1], "done")
		self.assertEqual(states[2], "running")
		self.assertEqual(states[3], "running")

	def test_running_marks_every_step_done(self):
		self.assertTrue(all(s == "done" for s in self._states("Running")))

	def test_failed_marks_deploy_phase_failed_rest_not_done(self):
		# Earlier phases done, the deploy phase failed, later steps pending — no
		# step after the failure reads "done".
		states = self._states("Failed")
		self.assertIn("failed", states)
		self.assertNotIn("done", states[states.index("failed") + 1 :])

	def test_unknown_status_degrades_to_all_pending_ish(self):
		# Never throws on a stray status (renders on a public-ish page).
		states = self._states("Nonsense")
		self.assertEqual(len(states), len(site_status.STEPS))

	def test_labels_are_user_facing_no_vm_jargon(self):
		joined = " ".join(s["label"].lower() for s in site_status.steps_for("Pending"))
		self.assertNotIn("vm", joined)
		self.assertNotIn("ssh", joined)


class TestPhasesFor(IntegrationTestCase):
	"""The merged, timed view: the six steps collapse onto the three phases the
	controller actually clocks, each with the seconds it took / is taking."""

	def _site(self, status, **stamps):
		# An in-memory Site stand-in: phases_for only reads `status` and the
		# `*_started` fields, so a plain _dict is enough (no DB round-trip).
		return frappe._dict({"name": "acme.blr1.frappe.dev", "subdomain": "acme", "status": status, **stamps})

	def _by_key(self, phases):
		return {p["key"]: p for p in phases}

	def test_three_phases_each_owning_two_steps(self):
		phases = site_status.phases_for(self._site("Pending"))
		self.assertEqual([p["key"] for p in phases], ["provisioning", "deploying", "running"])
		# Every one of the six checklist steps is claimed by exactly one phase.
		claimed = [s for p in phases for s in p["steps"]]
		self.assertEqual(sorted(claimed), sorted(s["key"] for s in site_status.STEPS))

	def test_pending_phases_have_no_timing(self):
		# Nothing has started — every phase's duration is unknown (None), not 0.
		phases = self._by_key(site_status.phases_for(self._site("Pending")))
		self.assertTrue(all(p["seconds"] is None for p in phases.values()))
		self.assertEqual(phases["provisioning"]["state"], "pending")

	def test_finished_phase_measures_start_to_next_stamp(self):
		# A Running site with all three stamps: each finished phase is the gap to
		# the next stamp (provisioning 40s, deploying 200s, running ~0).
		t0 = get_datetime("2026-06-10 10:00:00")
		site = self._site(
			"Running",
			provisioning_started=add_to_date(t0, seconds=0),
			deploying_started=add_to_date(t0, seconds=40),
			running_started=add_to_date(t0, seconds=240),
		)
		phases = self._by_key(site_status.phases_for(site))
		self.assertEqual(phases["provisioning"]["seconds"], 40)
		self.assertEqual(phases["deploying"]["seconds"], 200)
		self.assertEqual(phases["running"]["seconds"], 0)
		self.assertTrue(all(p["state"] == "done" for p in phases.values()))

	def test_in_flight_phase_counts_up_to_now(self):
		# Deploying, started 30s ago, no running stamp yet → measured to now, so
		# the live page counts up. provisioning is finished; running is None.
		now = frappe.utils.now_datetime()
		site = self._site(
			"Deploying",
			provisioning_started=add_to_date(now, seconds=-90),
			deploying_started=add_to_date(now, seconds=-30),
		)
		phases = self._by_key(site_status.phases_for(site))
		self.assertEqual(phases["provisioning"]["seconds"], 60)
		self.assertGreaterEqual(phases["deploying"]["seconds"], 29)
		self.assertEqual(phases["deploying"]["state"], "running")
		self.assertIsNone(phases["running"]["seconds"])

	def test_failed_phase_shows_elapsed_until_now_later_phases_none(self):
		# Deploy broke: it has a start but no end → elapsed-to-now; running never
		# started → None. The deploy phase rolls up to failed.
		now = frappe.utils.now_datetime()
		site = self._site(
			"Failed",
			provisioning_started=add_to_date(now, seconds=-120),
			deploying_started=add_to_date(now, seconds=-60),
		)
		phases = self._by_key(site_status.phases_for(site))
		self.assertEqual(phases["deploying"]["state"], "failed")
		self.assertGreaterEqual(phases["deploying"]["seconds"], 59)
		self.assertIsNone(phases["running"]["seconds"])

	def test_negative_delta_clamps_to_zero(self):
		# Clock skew / out-of-order stamps never surface a sub-zero duration.
		t0 = get_datetime("2026-06-10 10:00:00")
		site = self._site(
			"Running",
			provisioning_started=t0,
			deploying_started=add_to_date(t0, seconds=-5),  # earlier than start
			running_started=t0,
		)
		phases = self._by_key(site_status.phases_for(site))
		self.assertEqual(phases["provisioning"]["seconds"], 0)
