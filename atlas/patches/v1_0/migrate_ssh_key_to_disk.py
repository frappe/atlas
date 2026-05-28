"""For every existing Server Provider that still carries an in-DB
`ssh_private_key` (Password) column, write the decrypted key to disk at
`/etc/atlas/keys/<provider_name>.pem` and record the path on the new
`ssh_private_key_path` Data field.

The DocType JSON in this commit drops the Password column. This patch
must run in `[pre_model_sync]` so the column still exists when the
patch reads it; once `bench migrate` drops the column, the on-disk
copy and the new path column are the only remaining handles on the
key. Idempotent: re-running on an already-migrated row is a no-op.
"""

import pathlib

import frappe
import frappe.utils.password

DEFAULT_PATH_TEMPLATE = "/etc/atlas/keys/{provider_name}.pem"


def execute():
	if not frappe.db.has_column("Server Provider", "ssh_private_key"):
		return  # already migrated

	# Make sure the new column exists before we try to write to it. We're in
	# pre_model_sync, so the JSON-driven `bench migrate` add-column step
	# hasn't run yet. Adding it by hand is idempotent — Frappe's later sync
	# is a no-op when the column already matches.
	if not frappe.db.has_column("Server Provider", "ssh_private_key_path"):
		frappe.db.sql_ddl(
			"ALTER TABLE `tabServer Provider` ADD COLUMN `ssh_private_key_path` VARCHAR(255)"
		)

	for provider in frappe.get_all("Server Provider", pluck="name"):
		if frappe.db.get_value("Server Provider", provider, "ssh_private_key_path"):
			continue
		key = frappe.utils.password.get_decrypted_password(
			"Server Provider",
			provider,
			"ssh_private_key",
			raise_exception=False,
		)
		if not key:
			continue
		path = DEFAULT_PATH_TEMPLATE.format(provider_name=provider)
		target = pathlib.Path(path).expanduser()
		try:
			target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
			target.write_text(key)
			target.chmod(0o600)
		except PermissionError:
			# Fall back to a user-writable directory so a local-dev site that
			# runs as a non-root user can still complete the migration. The
			# operator can `sudo mv` to /etc/atlas/keys later.
			fallback_dir = pathlib.Path("~/.atlas/keys").expanduser()
			fallback_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
			path = str(fallback_dir / f"{provider}.pem")
			target = pathlib.Path(path)
			target.write_text(key)
			target.chmod(0o600)
		frappe.db.set_value(
			"Server Provider", provider, "ssh_private_key_path", path,
			update_modified=False,
		)
