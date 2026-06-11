"""Use case: every operator-visible button on the desk, driven through the
HTTP layer the desk actually uses.

The other use-case modules call controller methods in Python:
`provider.provision_server(...)`, `vm.start()`, etc. That covers the methods
but skips the layer that surfaces operator-visible failures — Frappe's
`/api/method/run_doc_method` endpoint that `frm.call(...)` posts to.

When the operator clicked buttons by hand, two failure shapes appeared:

1. **DigitalOcean errors at the API.** `provision_server` raises
   `DigitalOceanError` mid-call, before the `Server` row is inserted. The
   dialog stays open and the alert is the raw exception string. We want
   the throw to surface cleanly and not leave a half-written `Server`
   row.
2. **Mysterious failures.** Dialog fields ship strings to the server: the
   `Run Task` dialog's Code field posts `variables` as a JSON string, the
   `Sync to Server` dialog's Link field posts `server_name` as a string,
   the `Provision Server` dialog's Data field posts `server_name` as a
   string. Direct Python calls pass dicts / typed values and never
   exercise the string-decode paths.

This module drives every button on every form through
`frappe.handler.run_doc_method` with the **exact argument shape the desk
sends**: positional args as a dict, `variables` as a JSON string,
`server_name` as a Data string, etc. It also drives the negative paths an
operator can hit:

- `provision_server` with a bad DO token (401/403 from DO, no Server row
  left behind).
- `provision_server` with a duplicate name (ValidationError, no DO call).
- `Run Task` dialog with malformed JSON in the variables Code field.
- `Run Task` dialog with a script that's not in the catalogue.
- `Sync to Server` against a non-existent Server name (Link validation).
- `Start` / `Stop` / `Restart` / `Terminate` from the wrong state.

Cost: one shared bootstrapped server. The DO-error path uses a throwaway
provider whose token is `bogus`, so it never reaches the DO API
successfully — no droplet is created.

The happy paths intentionally overlap with the other use cases — but they
go through `run_doc_method`, so they record different code under
coverage and catch desk-only regressions (e.g. a method that stops being
whitelisted, an arg name that diverges from what JS sends).
"""

import json
import os
import tempfile
import time
from contextlib import contextmanager
from types import SimpleNamespace

import frappe
from frappe.handler import run_doc_method

from atlas.atlas.digitalocean import DigitalOceanError
from atlas.tests.e2e._shared import (
	ensure_image_on_server,
	ephemeral_public_key,
	expect_validation_error,
	phase,
	wait_for_vm_running,
)


def _bogus_key_path() -> str:
	"""Return an absolute path to a tempfile containing a bogus PEM-shaped
	string. The e2e negative-path provider only needs a path that resolves to
	an existing file; nothing ever SSHes with this key."""
	path = os.path.join(tempfile.gettempdir(), "atlas-e2e-bogus-key.pem")
	if not os.path.isfile(path):
		with open(path, "w") as handle:
			handle.write("-----BEGIN OPENSSH PRIVATE KEY-----\nbogus\n-----END OPENSSH PRIVATE KEY-----\n")
		os.chmod(path, 0o600)
	return path


def run(reuse: bool = True, keep: bool = True) -> None:
	with phase("desk-buttons", reuse=reuse, keep=keep) as server:
		image_doc = ensure_image_on_server(server.name)
		public_key = ephemeral_public_key()

		_check_provider_buttons(server)
		_check_server_buttons(server)
		_check_virtual_machine_image_buttons(server.name, image_doc.name)
		_check_virtual_machine_buttons(server.name, image_doc.name, public_key)
		_check_tls_buttons()
		_check_provision_server_bad_token()


def run_smoke(reuse: bool = True, keep: bool = True) -> None:
	"""Host-only path for development. Drives the operator buttons through the
	`run_doc_method` HTTP wrapper against a live server + booted VM — the layer
	the direct-Python use cases skip and the only reason this module exists.

	Runs the Server, Image, and Virtual Machine button maps (which include the
	wrong-state negatives inline, since they ride the same booted VM at no extra
	host cost). Skips `_check_provision_server_bad_token`: it mutates shared
	Atlas/DO Settings to prove a no-row-leak branch the controller unit test
	already covers, and the mutation is the documented can't-run-in-parallel
	hazard."""
	with phase("desk-buttons (smoke)", reuse=reuse, keep=keep) as server:
		image_doc = ensure_image_on_server(server.name)
		public_key = ephemeral_public_key()

		_check_server_buttons(server)
		_check_virtual_machine_image_buttons(server.name, image_doc.name)
		_check_virtual_machine_buttons(server.name, image_doc.name, public_key)


# ----- helpers -------------------------------------------------------------


@contextmanager
def _fake_post_request():
	"""Stand in for the WSGI request the desk would normally provide.

	`frappe.handler.is_valid_http_method` reads `frappe.local.request.method`
	to check the HTTP verb against `frappe.allowed_http_methods_for_whitelisted_func`.
	`bench execute` has no request, so we install a `SimpleNamespace` with
	`method="POST"` for the duration of the call and restore the previous
	binding on exit.
	"""
	sentinel = object()
	previous = getattr(frappe.local, "request", sentinel)
	frappe.local.request = SimpleNamespace(method="POST")
	try:
		yield
	finally:
		if previous is sentinel:
			# `frappe.local` is a Werkzeug Local; the attribute didn't exist
			# before, so remove it instead of writing `sentinel` back.
			try:
				del frappe.local.request
			except AttributeError:
				pass
		else:
			frappe.local.request = previous


def _call_button(doctype: str, name: str, method: str, **kwargs) -> object:
	"""Invoke a whitelisted controller method the way the desk does.

	`frm.call(method, args)` posts to `/api/method/run_doc_method` with
	`dt`, `dn`, `method`, and `args` (JSON-encoded dict). `run_doc_method`
	is a thin wrapper around `doc.run_method(method, **args)` that also
	checks `is_whitelisted`, the HTTP verb, and DocType read permission.

	We invoke `run_doc_method` directly so the test exercises the same
	wrapper. The return value lands in `frappe.response['message']`; we
	pop it off the response so the next call starts clean.
	"""
	frappe.response.pop("message", None)
	# `run_doc_method` mutates frappe.response.docs; clear it so we don't
	# accumulate stale entries across calls in the same test run.
	frappe.response.docs = []
	with _fake_post_request():
		run_doc_method(method=method, dt=doctype, dn=name, args=json.dumps(kwargs))
	return frappe.response.get("message")


# ----- Provider ------------------------------------------------------------


def _check_provider_buttons(server) -> None:
	"""Authenticate and Provision Server (happy + duplicate name)."""
	provider = frappe.get_doc("Provider", server.provider)

	# Authenticate: returns AuthResult-as-dict; either ok=True or ok=False
	# with an error message (e.g. for a bogus token).
	result = _call_button("Provider", provider.name, "authenticate")
	assert result and "ok" in result, result
	if not result["ok"]:
		error = result.get("error") or ""
		assert "401" in error or "403" in error or "forbidden" in error.lower(), error

	# Provision Server with a duplicate name: ValidationError, no DO call.
	# (The shared server's title is guaranteed to exist.)
	with expect_validation_error("already exists"):
		_call_button(
			"Provider",
			provider.name,
			"provision_server",
			title=server.title,
		)


# ----- Server --------------------------------------------------------------


def _check_server_buttons(server) -> None:
	"""Bootstrap, Run Task (dialog), Reboot is covered by run_task use case."""

	# Bootstrap: no args. Idempotent on an Active server.
	task_name = _call_button("Server", server.name, "bootstrap")
	assert task_name, "bootstrap returned no Task name"
	task = frappe.get_doc("Task", task_name)
	assert task.status == "Success", task.stderr

	# get_scripts: the desk picker only exposes the operator-visible subset
	# (sync-image). Lifecycle scripts that must run from a VM/Image
	# controller are filtered out so the operator can't fire terminate-vm.py
	# with empty variables from this menu. Bootstrap and reboot have their
	# own dedicated top-bar buttons with confirmation guards.
	scripts = _call_button("Server", server.name, "get_scripts")
	assert isinstance(scripts, list) and scripts, scripts
	names = {entry["name"] for entry in scripts}
	assert "sync-image.py" in names, names
	for hidden in (
		"bootstrap-server.py",
		"reboot-server.sh",
		"provision-vm.py",
		"start-vm.py",
		"stop-vm.py",
		"terminate-vm.py",
		"snapshot-vm.py",
		"rebuild-vm.py",
		"resize-vm.py",
		"pause-vm.py",
		"resume-vm.py",
		"delete-snapshot-vm.py",
	):
		assert hidden not in names, f"{hidden} leaked into operator-visible scripts: {names}"

	# Run Task dialog happy path. The Code field posts `variables` as a
	# JSON string, not a dict — drive that branch explicitly.
	task_name = _call_button(
		"Server",
		server.name,
		"run_task_dialog",
		script="bootstrap-server.py",
		variables=json.dumps(
			{
				"FIRECRACKER_VERSION": "v1.15.1",
				"ARCHITECTURE": "x86_64",
			}
		),
	)
	task = frappe.get_doc("Task", task_name)
	assert task.status == "Success", task.stderr

	# Run Task dialog with an empty variables string (operator clears the
	# Code field). `run_task_dialog` treats empty string as `{}`.
	with expect_validation_error("unknown script"):
		_call_button(
			"Server",
			server.name,
			"run_task_dialog",
			script="not-a-real-script.sh",
			variables="",
		)

	# Run Task dialog with malformed JSON in the variables Code field.
	# The operator typed `{foo: bar}` instead of `{"foo": "bar"}`. Pre-fix,
	# json.loads raised a bare JSONDecodeError that bubbled up as an opaque
	# 500. Post-fix, run_task_dialog re-throws it as a ValidationError that
	# the desk shows in a clean alert.
	with expect_validation_error("must be valid json"):
		_call_button(
			"Server",
			server.name,
			"run_task_dialog",
			script="bootstrap-server.py",
			variables="{not valid json",
		)

	# Run Task dialog with valid JSON that isn't an object.
	with expect_validation_error("variables must"):
		_call_button(
			"Server",
			server.name,
			"run_task_dialog",
			script="bootstrap-server.py",
			variables="[1, 2, 3]",
		)


# ----- Virtual Machine Image ----------------------------------------------


def _check_virtual_machine_image_buttons(server_name: str, image_name: str) -> None:
	"""Sync to Server (dialog) and Sync to All Servers."""

	# Sync to Server: server_name is a Link field, posted as a string.
	task_name = _call_button(
		"Virtual Machine Image",
		image_name,
		"sync_to_server",
		server_name=server_name,
	)
	assert task_name, "sync_to_server returned no Task name"
	# We don't wait for the queued Task to finish here — the image_sync use
	# case already covers the full run. We only assert the button enqueues.
	task = frappe.get_doc("Task", task_name)
	assert task.script == "sync-image.py", task.script
	assert task.server == server_name, task.server
	assert task.status in ("Pending", "Running", "Success"), task.status

	# Sync to All Servers: returns one Task name per Active Server row.
	tasks = _call_button(
		"Virtual Machine Image",
		image_name,
		"sync_to_all_servers",
	)
	active_count = frappe.db.count("Server", filters={"status": "Active"})
	assert isinstance(tasks, list) and len(tasks) == active_count, (tasks, active_count)


# ----- Virtual Machine -----------------------------------------------------


def _check_virtual_machine_buttons(server_name: str, image_name: str, public_key: str) -> None:
	"""Auto-provision (insert -> Running) -> Stop -> Start -> Restart ->
	Terminate, every step via `run_doc_method`. Mirrors the JS button map in
	`virtual_machine.js`. Per Phase 4 the form's Provision button is gone on
	Pending — `after_insert` enqueues `auto_provision`."""

	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "desk-buttons lifecycle",
			"server": server_name,
			"image": image_name,
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 4,
			"ssh_public_key": public_key,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	# Auto-provision worker flips Pending -> Running. No explicit button click.
	wait_for_vm_running(vm.name, timeout_seconds=120)
	vm.reload()
	assert vm.status == "Running", vm.status

	# Stop.
	_call_button("Virtual Machine", vm.name, "stop")
	vm.reload()
	assert vm.status == "Stopped", vm.status

	# Start.
	time.sleep(1)
	_call_button("Virtual Machine", vm.name, "start")
	vm.reload()
	assert vm.status == "Running", vm.status

	# Stop (memory snapshot) — the one-click fast stop posts the exact arg
	# shape the desk sends ({"memory_snapshot": true}); the next plain Start
	# must consume the snapshot (restore it on the host) and clear the flag.
	time.sleep(1)
	_call_button("Virtual Machine", vm.name, "stop", memory_snapshot=True)
	vm.reload()
	assert vm.status == "Stopped", vm.status
	assert vm.has_memory_snapshot, "fast stop should have captured a memory snapshot"
	time.sleep(1)
	_call_button("Virtual Machine", vm.name, "start")
	vm.reload()
	assert vm.status == "Running", vm.status
	assert not vm.has_memory_snapshot, "start should consume the memory snapshot"

	# Restart returns a {stop_task, start_task} pair.
	time.sleep(1)
	result = _call_button("Virtual Machine", vm.name, "restart")
	assert result and result.get("stop_task") and result.get("start_task"), result
	vm.reload()
	assert vm.status == "Running", vm.status

	_check_pause_resume_buttons(vm)
	_check_snapshot_family_buttons(vm)

	# Terminate (from Stopped, where the snapshot-family checks leave it).
	_call_button("Virtual Machine", vm.name, "terminate")
	vm.reload()
	assert vm.status == "Terminated", vm.status

	# Terminated state guard: every button is gone in the JS map; if the
	# operator races a stale tab and clicks Terminate again, the server
	# must throw rather than re-running the script.
	with expect_validation_error("already terminated"):
		_call_button("Virtual Machine", vm.name, "terminate")


def _check_pause_resume_buttons(vm) -> None:
	"""Pause / Resume through run_doc_method, plus the wrong-state negatives.
	Enters with the VM Running; leaves it Running."""
	# Resume from Running is rejected.
	with expect_validation_error("cannot resume"):
		_call_button("Virtual Machine", vm.name, "resume")

	_call_button("Virtual Machine", vm.name, "pause")
	vm.reload()
	assert vm.status == "Paused", vm.status

	# Pause again from Paused is rejected.
	with expect_validation_error("cannot pause"):
		_call_button("Virtual Machine", vm.name, "pause")

	_call_button("Virtual Machine", vm.name, "resume")
	vm.reload()
	assert vm.status == "Running", vm.status


def _check_snapshot_family_buttons(vm) -> None:
	"""Snapshot / Restore / Rebuild / Resize / Clone and their dialog-shaped
	arguments and negatives. Enters Running; leaves the VM Stopped."""
	# Snapshot is rejected while Running — operator must stop first.
	with expect_validation_error("stop the vm before snapshotting"):
		_call_button("Virtual Machine", vm.name, "snapshot", title="too early")
	# Resize is rejected while Running.
	with expect_validation_error("stop the vm before resizing"):
		_call_button("Virtual Machine", vm.name, "resize", vcpus=2)

	_call_button("Virtual Machine", vm.name, "stop")
	vm.reload()
	assert vm.status == "Stopped", vm.status

	# Snapshot (Stopped). Title posts as a Data string from the prompt.
	snapshot_name = _call_button("Virtual Machine", vm.name, "snapshot", title="desk snap")
	assert snapshot_name, "snapshot returned no name"
	snapshot = frappe.get_doc("Virtual Machine Snapshot", snapshot_name)
	assert snapshot.status == "Available", snapshot.status

	# Restore the snapshot onto its own VM (Snapshot form's button).
	restore_task = _call_button("Virtual Machine Snapshot", snapshot_name, "restore_to_vm")
	assert restore_task, "restore_to_vm returned no Task"
	vm.reload()
	assert vm.status == "Stopped", vm.status

	# Rebuild from image (source posts as strings the dialog sends).
	rebuild_task = _call_button("Virtual Machine", vm.name, "rebuild", source_type="image", source=vm.image)
	assert rebuild_task, "rebuild returned no Task"

	# Rebuild with an unknown source_type is rejected cleanly.
	with expect_validation_error("unknown rebuild source_type"):
		_call_button("Virtual Machine", vm.name, "rebuild", source_type="banana")

	# Resize (Stopped). Int fields post as strings from the prompt.
	resize_task = _call_button(
		"Virtual Machine",
		vm.name,
		"resize",
		vcpus="2",
		memory_megabytes="1024",
		disk_gigabytes="6",
	)
	assert resize_task, "resize returned no Task"
	vm.reload()
	assert vm.vcpus == 2 and vm.disk_gigabytes == 6, (vm.vcpus, vm.disk_gigabytes)

	# Disk shrink rejected.
	with expect_validation_error("can only grow"):
		_call_button("Virtual Machine", vm.name, "resize", disk_gigabytes="4")

	# Clone into a new VM (Snapshot form's button). Returns the new VM name.
	clone_name = _call_button(
		"Virtual Machine Snapshot",
		snapshot_name,
		"clone_to_new_vm",
		title="desk clone",
		ssh_public_key=ephemeral_public_key(),
	)
	assert clone_name and clone_name != vm.name, clone_name
	clone = frappe.get_doc("Virtual Machine", clone_name)
	assert clone.clone_source_rootfs == snapshot.rootfs_path
	# `_call_button` runs the whitelisted method in-process and does NOT commit
	# (a real HTTP request would). Commit the clone row now: the enqueued
	# auto_provision worker is a separate process that can only load a committed
	# row, and wait_for_vm_running's first act is frappe.db.rollback() — without
	# this commit that rollback discards the uncommitted insert and the VM 404s.
	frappe.db.commit()
	# Don't wait for the clone to provision here — the snapshot use case covers
	# the full clone boot. Terminate it so it doesn't linger on the shared box.
	wait_for_vm_running(clone.name, timeout_seconds=120)
	clone.reload()
	clone.terminate()

	# Delete the snapshot row (cascades the on-host file delete). The VM is
	# still alive, so on_trash runs delete-snapshot-vm.py.
	frappe.delete_doc("Virtual Machine Snapshot", snapshot_name, ignore_permissions=True)
	assert not frappe.db.exists("Virtual Machine Snapshot", snapshot_name)


# ----- Provision Server with a bad token ----------------------------------


def _check_provision_server_bad_token() -> None:
	"""DO returns 401 on a bogus token; the dialog's Provision click raises
	DigitalOceanError. Critically: no `Server` row is inserted and no
	droplet leaks, because the throw happens before the row insert.

	This path covers the DO-API-rejects-us branch the operator hit when
	their token had expired. We temporarily swap `Atlas Settings.provider`
	to a throwaway Provider row and clobber `DigitalOcean Settings.api_token`
	with a bogus value, then restore both. The mutation is shared state,
	so this test cannot run in parallel with other use cases — a guard
	rail if e2e parallelism ever lands.
	"""
	import frappe.utils.password

	provider_name = "atlas-e2e-bogus-token"
	if not frappe.db.exists("Provider", provider_name):
		frappe.get_doc(
			{
				"doctype": "Provider",
				"provider_name": provider_name,
				"provider_type": "DigitalOcean",
				"is_active": 1,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	previous_provider = frappe.db.get_single_value("Atlas Settings", "provider")
	previous_token = frappe.utils.password.get_decrypted_password(
		"DigitalOcean Settings",
		"DigitalOcean Settings",
		"api_token",
		raise_exception=False,
	)
	frappe.db.set_single_value("Atlas Settings", "provider", provider_name, update_modified=False)
	frappe.utils.password.set_encrypted_password(
		"DigitalOcean Settings",
		"DigitalOcean Settings",
		"do_v1_bogus_token_for_negative_path",
		"api_token",
	)
	frappe.db.commit()

	target_title = f"atlas-e2e-badtoken-{int(time.time())}"
	caught = False
	try:
		_call_button(
			"Provider",
			provider_name,
			"provision_server",
			title=target_title,
		)
	except DigitalOceanError as exception:
		caught = True
		message = str(exception).lower()
		assert "401" in message or "403" in message or "unauthorized" in message, message
	finally:
		if previous_provider:
			frappe.db.set_single_value("Atlas Settings", "provider", previous_provider, update_modified=False)
		if previous_token:
			frappe.utils.password.set_encrypted_password(
				"DigitalOcean Settings",
				"DigitalOcean Settings",
				previous_token,
				"api_token",
			)
		frappe.db.commit()
		assert not frappe.db.exists("Server", {"title": target_title}), (
			f"Server row with title {target_title!r} leaked despite DO API failure"
		)
	assert caught, "provision_server with a bogus token should have raised"


# ----- TLS & Domain layer --------------------------------------------------


def _check_tls_buttons() -> None:
	"""Domain Provider / TLS Provider / Root Domain / TLS Certificate buttons,
	all through `run_doc_method` — the desk-layer regression net (a method that
	stops being whitelisted, an arg name that diverges from the JS).

	The external edges are mocked so this needs no certbot, no AWS, no proxy VM:
	the TLS provider's `issue()` returns canned PEM paths and `proxy.push_cert` is
	a no-op. What this proves is the HTTP wrapper: the methods are whitelisted, the
	arg shapes match, and the issue->cert->(would-)push chain runs end to end through
	the controllers. The unit suite covers the real fan-out and the providers; this
	covers the desk seam. Self-contained: it creates and tears down its own rows."""
	from unittest.mock import patch

	from atlas.atlas.doctype.tls_certificate import tls_certificate as cert_module
	from atlas.atlas.tls.base import IssuedCert

	stamp = int(time.time())
	domain = f"e2e-{stamp}.frappe.dev"
	region = f"e2e-{stamp}"
	dp_name = "atlas-e2e-route53"
	tp_name = "atlas-e2e-letsencrypt"
	created_certs: list[str] = []

	def _cleanup() -> None:
		for cert in created_certs:
			if frappe.db.exists("TLS Certificate", cert):
				frappe.delete_doc("TLS Certificate", cert, force=1, ignore_permissions=True)
		for dt, name in (("Root Domain", domain), ("Domain Provider", dp_name), ("TLS Provider", tp_name)):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=1, ignore_permissions=True)
		frappe.db.commit()

	_cleanup()
	# Dummy provider credentials so Test Connection runs its real auth path (and
	# fails cleanly on the bogus creds / missing boto3) rather than throwing on a
	# missing-secret read. Mirrors the operator order: configure, then test.
	# `from … import` (not `import frappe.utils.password`) so we don't rebind the
	# module-level `frappe` to a function local — the _cleanup closure reads it.
	from frappe.utils.password import set_encrypted_password

	set_encrypted_password(
		"Route53 Settings", "Route53 Settings", "atlas-e2e-bogus-secret", "secret_access_key"
	)
	frappe.db.set_single_value("Route53 Settings", "access_key_id", "AKIAE2EBOGUS", update_modified=False)
	frappe.db.set_single_value(
		"Lets Encrypt Settings", "account_email", "e2e@frappe.dev", update_modified=False
	)
	frappe.db.set_single_value("Lets Encrypt Settings", "agree_tos", 1, update_modified=False)
	frappe.db.commit()

	frappe.get_doc(
		{"doctype": "Domain Provider", "provider_name": dp_name, "provider_type": "Route53"}
	).insert(ignore_permissions=True)
	frappe.get_doc(
		{"doctype": "TLS Provider", "provider_name": tp_name, "provider_type": "Let's Encrypt"}
	).insert(ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": "Root Domain",
			"domain": domain,
			"region": region,
			"domain_provider": dp_name,
			"tls_provider": tp_name,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	try:
		# Test Connection on both providers: AuthResult-as-dict with an "ok" key.
		# Real AWS/ACME isn't configured here, so ok is allowed to be False with an
		# error — what matters is the wrapper returns the dict shape.
		for provider_dt, provider_name in (("Domain Provider", dp_name), ("TLS Provider", tp_name)):
			result = _call_button(provider_dt, provider_name, "authenticate")
			assert result and "ok" in result, (provider_dt, result)

		# Issue / Renew Certificate on Root Domain: creates the cert and runs the
		# issue chain (mocked issuer + mocked push). Returns the cert name.
		issued = IssuedCert(
			fullchain_path="/dev/null",
			privkey_path="/dev/null",
			not_before="2026-06-08 00:00:00",
			not_after="2026-09-06 00:00:00",
		)
		with (
			patch.object(cert_module.tls, "for_tls_provider") as tls_for,
			patch.object(cert_module.proxy, "push_cert"),
			# /dev/null isn't a readable PEM; short-circuit the on-disk read.
			patch.object(cert_module, "_read_pem", return_value="PEM"),
		):
			tls_for.return_value.issue.return_value = issued
			cert_name = _call_button("Root Domain", domain, "issue_certificate")
			assert cert_name, "issue_certificate should return a TLS Certificate name"
			created_certs.append(cert_name)

			# Push to Proxies on the issued cert: no proxies in this throwaway
			# region, so the result is an empty list — but the wrapper + read path run.
			pushed = _call_button("TLS Certificate", cert_name, "push_to_proxies")
			assert pushed == [], pushed

		# Archive flips is_active on both providers (the danger button).
		_call_button("Domain Provider", dp_name, "archive")
		assert not frappe.db.get_value("Domain Provider", dp_name, "is_active")
		_call_button("TLS Provider", tp_name, "archive")
		assert not frappe.db.get_value("TLS Provider", tp_name, "is_active")
	finally:
		_cleanup()
