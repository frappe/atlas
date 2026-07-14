"""S3 snapshot backup round-trip — upload a Cold snapshot to S3, lose the local
LV, restore it from S3, and roll the VM back onto the rehydrated disk.

The use case behind [spec/29-snapshot-backup.md](../../../../spec/29-snapshot-backup.md):
a `Virtual Machine Snapshot` is one LVM thin snapshot in one pool on one host —
instant and space-thin, but not durable (lose the pool and every snapshot on it
is gone). This proves the off-host round trip against a real droplet + a real S3
endpoint — the host facts nothing but a live run can show:

  1. upload: upload-snapshot-s3.py activates the snapshot LV, `zstd -o`s it to a
     temp (reading the block device directly — no dd, no pipe, so the exit code is
     honestly zstd's own), `sha256sum`s it, and `curl -T`s it to a
     controller-presigned PUT URL. The host holds NO S3 credentials.
  2. durability: `lvremove` the snapshot LV on the host — the pool no longer has
     it, exactly as a wiped disk / re-imaged host would leave it.
  3. restore: restore-snapshot-s3.py re-presigns a GET per manifest object,
     recreates a fresh thin LV at the exact name the row records
     (`atlas-snap-<id>`), verifies each object's sha256 BEFORE decompressing (the
     sync-image integrity gate), and `zstd -d --sparse`s it back onto the LV. A
     Cold restore then chains restore_to_vm() to roll the VM back in place.
  4. proof: the VM starts and boots off the rebuilt disk — the snapshot survived
     the loss of its local LV.

Needs an `s3` block in the E2E fixture ($ATLAS_E2E_CONFIG, default
`~/.cache/atlas-e2e/config.json`); without it the module skips cleanly
(MissingConfig) rather than failing deep in boto3. A local MinIO is the zero-cost
target — point `s3.endpoint_url` at it (path-style is auto for a custom endpoint;
see spec/29). Heavy (a real VM provision + a multi-GB round trip), so it is
invoked directly, not folded into `run_all_smoke`:

    bench --site <site> execute \
        atlas.tests.e2e.use_cases.snapshot_backup.run_smoke

The VM + snapshot are torn down in a `finally`; the snapshot's on_trash also
sweeps its S3 objects, so a run leaves no paid orphan in the bucket. It needs the
background worker up (the VM auto-provision contract), like the other host e2es.
"""

import json

import frappe

from atlas.atlas.ssh import run_task
from atlas.tests.e2e._config import E2EConfig, MissingConfig
from atlas.tests.e2e._droplets import ensure_e2e_provider
from atlas.tests.e2e._shared import (
	assert_probe,
	ensure_image_on_server,
	ephemeral_public_key,
	phase,
	wait_for_vm_running,
)


def run_smoke(reuse: bool = True, keep: bool = True) -> None:
	with phase("snapshot-s3-backup", reuse=reuse, keep=keep) as server:
		# Skips cleanly (MissingConfig) on a box without an `s3` fixture block.
		_configure_s3()
		# Unit tests may have clobbered Atlas/DigitalOcean Settings with fakes;
		# re-seed from the fixture before anything SSHes (real-provision-traps).
		ensure_e2e_provider()
		# The two S3 host verbs + the durable atlas lib refresh via scp, not
		# per-Task — an Active server from a prior run needs the sync.
		uploaded = server.sync_scripts()
		print(f"[s3] sync_scripts: {uploaded} durable files refreshed on {server.name}")
		image = ensure_image_on_server(server.name)

		virtual_machine = _provision_stopped_vm(server.name, image.name)
		snapshot = None
		try:
			snapshot = frappe.get_doc("Virtual Machine Snapshot", _snapshot(virtual_machine))
			_upload_and_verify(snapshot)
			_lose_local_lv(server.name, snapshot)
			_restore_and_verify(server.name, virtual_machine, snapshot)
		finally:
			_teardown(virtual_machine, snapshot)


def _configure_s3() -> None:
	"""Seed `S3 Settings` from the fixture's `s3` block via the idempotent setup()
	contract — the test-side analogue of `ensure_e2e_provider`, so the harness
	drives the same explicit setter the operator's Test Connection button does.

	Raises MissingConfig naming what to add when the block is absent, so a box that
	hasn't configured S3 skips this e2e cleanly rather than failing deep in boto3.
	"""
	config = E2EConfig.load()
	if not config.section("s3"):
		raise MissingConfig(
			"e2e config has no 's3' block — add {bucket, access_key_id, "
			"secret_access_key, endpoint_url?} to run the snapshot-S3 e2e. A local "
			"MinIO is the zero-cost target (endpoint_url http://127.0.0.1:9000); see spec/29."
		)
	frappe.get_single("S3 Settings").setup(
		bucket=config.require_in("s3", "bucket"),
		access_key_id=config.require_in("s3", "access_key_id"),
		secret_access_key=config.require_in("s3", "secret_access_key"),
		region=config.section("s3").get("region") or "us-east-1",
		endpoint_url=config.section("s3").get("endpoint_url") or "",
		key_prefix=config.section("s3").get("key_prefix") or "atlas/snapshots",
	)
	frappe.db.commit()

	from atlas.atlas import s3

	result = s3.S3Backup().test_connection()
	assert result.ok, f"S3 Test Connection failed — check the bucket/credentials/endpoint: {result.detail}"
	print(f"[s3] S3 Settings configured: {result.detail}")


def _provision_stopped_vm(server_name: str, image: str) -> str:
	"""Provision a small VM, wait for it to boot, then Stop it — the clean base
	state for a Cold snapshot (Stopped-only, the safe default) and the Cold restore
	rollback (rebuild() needs a Stopped VM). Returns the VM name."""
	virtual_machine = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "snapshot-s3-e2e",
			"server": server_name,
			"image": image,
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 4,
			"ssh_public_key": ephemeral_public_key(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	# Phase 4 auto-provision: after_insert enqueues provision(); the worker drives
	# Pending -> Running. Then stop for a flush-consistent disk to snapshot.
	wait_for_vm_running(virtual_machine.name, timeout_seconds=180)
	virtual_machine.reload()
	virtual_machine.stop()
	virtual_machine.reload()
	assert virtual_machine.status == "Stopped", virtual_machine.status
	print(f"[s3] {virtual_machine.name} provisioned and Stopped — ready to snapshot")
	return virtual_machine.name


def _snapshot(virtual_machine_name: str) -> str:
	"""Take a Cold (disk-only) snapshot of the Stopped VM. Returns its name."""
	virtual_machine = frappe.get_doc("Virtual Machine", virtual_machine_name)
	name = virtual_machine.snapshot("s3-e2e-backup")
	frappe.db.commit()
	snapshot = frappe.get_doc("Virtual Machine Snapshot", name)
	assert snapshot.kind == "Cold", snapshot.kind
	assert snapshot.status == "Available", snapshot.status
	print(f"[s3] cold snapshot {name} (rootfs {snapshot.rootfs_path})")
	return name


def _upload_and_verify(snapshot) -> None:
	"""Drive the upload host Task inline (deterministic logs; the real per-object
	zstd/sha256/curl still runs on the host), then assert the row records a manifest
	and the objects really landed in the bucket."""
	# _run_upload is the background half of upload_to_s3 — called directly so this
	# process, not a worker, carries the Task, and the assertions see the result.
	snapshot._run_upload()
	snapshot.reload()
	assert snapshot.s3_status == "Uploaded", snapshot.s3_status
	manifest = json.loads(snapshot.s3_objects or "[]")
	assert manifest, "upload recorded no manifest"
	assert int(snapshot.s3_size_bytes or 0) > 0, snapshot.s3_size_bytes
	for obj in manifest:
		# Every object carries a sha256 of its COMPRESSED bytes (the restore gate).
		assert obj["sha256"], f"manifest object {obj['name']} has no sha256"
	print(
		f"[s3] uploaded {len(manifest)} object(s), {snapshot.s3_size_bytes} compressed bytes "
		f"to s3://{snapshot.s3_bucket}/{snapshot.s3_key_prefix}"
	)
	_assert_objects_in_bucket(snapshot, manifest)


def _assert_objects_in_bucket(snapshot, manifest: list[dict]) -> None:
	"""Controller-side head_object per manifest object — independent proof the bytes
	reached S3, not just that the host reported success."""
	from atlas.atlas import s3

	backup = s3.S3Backup()
	client = backup._client()
	for obj in manifest:
		key = backup.object_key(snapshot.name, obj["object_name"])
		head = client.head_object(Bucket=backup.bucket, Key=key)
		assert head["ContentLength"] == obj["compressed_bytes"], (
			f"{key}: bucket size {head['ContentLength']} != manifest {obj['compressed_bytes']}"
		)
	print(f"[s3] verified {len(manifest)} object(s) present in the bucket")


def _lose_local_lv(server_name: str, snapshot) -> None:
	"""Simulate losing the pool: lvremove the snapshot LV on the host WITHOUT
	deleting the row. Reuses the on_trash host verb (delete-snapshot-vm) so the
	loss is exactly what a wiped disk leaves — the row still points at LV names
	that no longer exist on the host, which is what the restore must rebuild."""
	task = run_task(
		server=server_name,
		script="delete-snapshot-vm",
		variables={
			"SNAPSHOT_ROOTFS_PATH": snapshot.rootfs_path,
			"DATA_SNAPSHOT_ROOTFS_PATH": snapshot.data_rootfs_path or "",
			# Cold snapshot: no memory pair to remove.
			"MEMORY_DIRECTORY": "",
		},
		virtual_machine=snapshot.virtual_machine,
		timeout_seconds=60,
	)
	assert task.status == "Success", task.stderr
	print(f"[s3] dropped local snapshot LV {snapshot.rootfs_path} — the pool no longer holds it")


def _restore_and_verify(server_name: str, virtual_machine_name: str, snapshot) -> None:
	"""Restore the snapshot from S3 (rehydrate the LV + roll the Cold VM back), then
	prove the VM boots off the rebuilt disk."""
	snapshot.reload()
	assert snapshot.s3_status == "Uploaded", snapshot.s3_status
	# _run_restore is the background half of restore_from_s3: rehydrate the LV, then
	# (Cold) chain restore_to_vm() and return its rollback Task name.
	rollback_task = snapshot._run_restore()
	snapshot.reload()
	assert snapshot.s3_status == "Uploaded", snapshot.s3_status
	assert rollback_task, "a Cold restore should return an in-place rollback Task"
	print(f"[s3] restored from S3 and rolled {virtual_machine_name} back (task {rollback_task})")

	# rebuild() leaves the VM Stopped; start it and prove the unit comes up on the
	# disk that was just rebuilt from the S3-restored snapshot.
	virtual_machine = frappe.get_doc("Virtual Machine", virtual_machine_name)
	virtual_machine.start()
	virtual_machine.reload()
	assert virtual_machine.status == "Running", virtual_machine.status
	assert_probe(server_name, "phase5-is-active", VIRTUAL_MACHINE_NAME=virtual_machine_name)
	print(f"[s3] {virtual_machine_name} boots off the S3-restored disk — round trip verified")


def _teardown(virtual_machine_name: str, snapshot) -> None:
	"""Delete the snapshot (on_trash lvremoves the LV AND sweeps the S3 objects) and
	terminate the VM. Best-effort — teardown must not mask the real failure."""
	if snapshot is not None:
		try:
			frappe.delete_doc("Virtual Machine Snapshot", snapshot.name, ignore_permissions=True, force=True)
			frappe.db.commit()
		except Exception as error:
			print(f"[s3] snapshot teardown failed for {snapshot.name}: {error}")
	try:
		virtual_machine = frappe.get_doc("Virtual Machine", virtual_machine_name)
		if virtual_machine.status != "Terminated":
			virtual_machine.terminate()
		frappe.db.commit()
	except Exception as error:
		print(f"[s3] vm teardown failed for {virtual_machine_name}: {error}")
