from __future__ import annotations

from pathlib import Path

import frappe
from frappe.flow.knowledge import Knowledge

from atlas.operations.investigator.agents import KNOWLEDGE_BASE

SOURCE_PATTERNS = ("docs/**/*.md", "**/SPEC.md")
SKIPPED_FOLDERS = {"node_modules", ".git", "dist", ".tmp"}


def load_knowledge_base() -> dict[str, int]:
	"""Load the Atlas handbook, the specifications, and the incident reports into the Atlas knowledge base.

	A source keeps its repository path as its title. Atlas adds new files, reloads changed files, and deletes
	sources of removed files. Run it after a deploy that changes the documents.
	"""
	root = Path(frappe.get_app_source_path("atlas"))
	knowledge = Knowledge(KNOWLEDGE_BASE)
	documents = {
		str(path.relative_to(root)): path.read_text(encoding="utf-8")
		for pattern in SOURCE_PATTERNS
		for path in sorted(root.glob(pattern))
		if not SKIPPED_FOLDERS & set(path.relative_to(root).parts)
	}
	sources = {
		source.title: source
		for source in frappe.get_all(
			"Flow Knowledge Source",
			filters={"knowledge_base": knowledge.name},
			fields=["name", "title", "content"],
		)
	}

	counts = {"added": 0, "reloaded": 0, "deleted": 0, "unchanged": 0}
	for title, source in sources.items():
		if documents.get(title) != source.content:
			frappe.delete_doc("Flow Knowledge Source", source.name, ignore_permissions=True)
			counts["deleted" if title not in documents else "reloaded"] += 1

	for title, content in documents.items():
		if title in sources and sources[title].content == content:
			counts["unchanged"] += 1
		elif content.strip():
			knowledge.add_text(content, title=title)
			counts["added"] += title not in sources

	return counts
