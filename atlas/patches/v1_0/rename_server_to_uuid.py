"""Switch `Server` from slug-named to UUID-named, keeping the old slug
as the human-readable `title`.

Runs in `pre_model_sync` so the legacy `server_name` column is still in
place when we rename it. After this patch:

  - The `tabServer` table has a `title` column carrying the old slug.
  - Every row's `name` is a UUID.
  - FK references on `tabTask.server`, `tabVirtual Machine.server`, and
    `tabVirtual Machine Image Sync.server` (if present) follow via raw
    SQL updates.

We deliberately avoid `frappe.rename_doc` here because at pre_model_sync
time the in-memory DocType meta still reflects the legacy
`autoname: field:server_name` rule, which trips on the renamed column.

Idempotent: re-running on an already-migrated table is a no-op.
"""

import uuid

import frappe


# `tabDocType.column_name` pairs that hold an FK reference to a Server
# row. Update in raw SQL so rename_doc's autoname machinery doesn't fire.
FK_TARGETS = (
	("tabTask", "server"),
	("tabVirtual Machine", "server"),
)


def execute() -> None:
	has_server_name = frappe.db.has_column("Server", "server_name")
	has_title = frappe.db.has_column("Server", "title")

	if has_title and not has_server_name:
		_assign_uuid_names()
		return

	if has_server_name and not has_title:
		# Column rename: server_name → title. SQL preserves the value.
		frappe.db.sql_ddl(
			"ALTER TABLE `tabServer` CHANGE `server_name` `title` VARCHAR(140)"
		)
	elif has_server_name and has_title:
		# Both columns exist after an earlier partial migration. Backfill
		# `title` from `server_name` where empty; leave both columns until
		# the next migrate cycle drops `server_name`.
		frappe.db.sql(
			"UPDATE `tabServer` SET `title` = `server_name` "
			"WHERE (`title` IS NULL OR `title` = '') AND `server_name` IS NOT NULL"
		)

	_assign_uuid_names()


def _assign_uuid_names() -> None:
	"""For every Server row whose `name` is not already a UUID, mint a
	new UUID, update the row, and rewrite every FK pointer in raw SQL."""
	rows = frappe.db.sql("SELECT name FROM `tabServer`", as_dict=True)
	for row in rows:
		old_name = row["name"]
		if _is_uuid(old_name):
			continue
		new_name = str(uuid.uuid4())
		frappe.db.sql(
			"UPDATE `tabServer` SET `name` = %s WHERE `name` = %s",
			(new_name, old_name),
		)
		for table, column in FK_TARGETS:
			if not _table_exists(table):
				continue
			frappe.db.sql(
				f"UPDATE `{table}` SET `{column}` = %s WHERE `{column}` = %s",
				(new_name, old_name),
			)
	frappe.db.commit()


def _is_uuid(value: str) -> bool:
	try:
		uuid.UUID(value)
		return True
	except (ValueError, AttributeError, TypeError):
		return False


def _table_exists(table: str) -> bool:
	return bool(
		frappe.db.sql(
			"SELECT 1 FROM information_schema.tables "
			"WHERE table_schema = DATABASE() AND table_name = %s",
			(table,),
		)
	)
