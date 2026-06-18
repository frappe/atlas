"""Central API client.

Central is the global control plane (spec/16-central.md). One Central manages
many Atlas instances; Atlas is the *client*. This is the inverse of the Provider
relationship — so the client mirrors atlas/atlas/digitalocean.py: a thin
requests wrapper, one *Error type, dataclasses for the typed responses.

Central exposes whitelisted Frappe methods. Atlas calls them at
`<url>/api/method/central.api.<name>` with a token header. The route names live
here and nowhere else, so a contract change on Central's side is a one-file edit.

The upsert helpers (upsert_central_sizes / upsert_central_images) mirror
provider.upsert_catalog: insert-or-update, then disable rows Central no longer
lists.
"""

from __future__ import annotations

import dataclasses

import frappe
import requests

DEFAULT_TIMEOUT = 30

# Central method routes. Pinned in one place — the wire contract from
# spec/16-central.md § "The wire contract".
_ROUTES = {
	"ping": "central.api.ping",
	"register": "central.api.register",
	"sizes": "central.api.sizes",
	"images": "central.api.images",
	"event": "central.api.event",
}


class CentralError(Exception):
	pass


@dataclasses.dataclass(frozen=True, slots=True)
class CentralAuthResult:
	ok: bool
	label: str | None = None
	error: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class RegistrationResult:
	atlas_id: str
	label: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class CentralSizeInfo:
	slug: str
	title: str
	vcpus: int
	cpu_max_cores: float
	memory_megabytes: int
	disk_gigabytes: int
	monthly_cost_usd: int | None = None
	central_metadata: dict | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class CentralImageInfo:
	image_name: str
	title: str
	series: str | None = None
	central_metadata: dict | None = None


class CentralClient:
	"""Talks to a single Central instance. Constructed from Central Settings."""

	def __init__(self, url: str, api_key: str, api_secret: str, timeout: int = DEFAULT_TIMEOUT):
		self.url = url.rstrip("/")
		self.api_key = api_key
		self.api_secret = api_secret
		self.timeout = timeout

	def ping(self) -> CentralAuthResult:
		"""Credential check. Never raises — returns ok=False so the Test
		Connection toast can render a red indicator."""
		try:
			body = self._request("GET", "ping")
		except CentralError as exception:
			return CentralAuthResult(ok=False, error=str(exception))
		return CentralAuthResult(ok=True, label=body.get("label"))

	def register(self, payload: dict) -> RegistrationResult:
		body = self._request("POST", "register", json=payload)
		atlas_id = body.get("atlas_id")
		if not atlas_id:
			raise CentralError("Central register returned no atlas_id")
		return RegistrationResult(atlas_id=atlas_id, label=body.get("label"))

	def fetch_sizes(self) -> tuple[CentralSizeInfo, ...]:
		rows = self._request("GET", "sizes").get("sizes", [])
		return tuple(
			CentralSizeInfo(
				slug=row["slug"],
				title=row.get("title") or row["slug"],
				vcpus=int(row.get("vcpus") or 0),
				cpu_max_cores=float(row.get("cpu_max_cores") or 0),
				memory_megabytes=int(row.get("memory_megabytes") or 0),
				disk_gigabytes=int(row.get("disk_gigabytes") or 0),
				monthly_cost_usd=row.get("monthly_cost_usd"),
				central_metadata=row,
			)
			for row in rows
		)

	def fetch_images(self) -> tuple[CentralImageInfo, ...]:
		rows = self._request("GET", "images").get("images", [])
		return tuple(
			CentralImageInfo(
				image_name=row["image_name"],
				title=row.get("title") or row["image_name"],
				series=row.get("series"),
				central_metadata=row,
			)
			for row in rows
		)

	def post_event(self, event: dict) -> None:
		self._request("POST", "event", json=event)

	def _request(self, method: str, route_key: str, json: dict | None = None) -> dict:
		url = f"{self.url}/api/method/{_ROUTES[route_key]}"
		headers = {
			"Authorization": f"token {self.api_key}:{self.api_secret}",
			"Content-Type": "application/json",
			"Accept": "application/json",
		}
		try:
			response = requests.request(method, url, json=json, headers=headers, timeout=self.timeout)
		except requests.RequestException as exception:
			raise CentralError(f"{method} {route_key}: {exception}") from exception
		if response.status_code >= 400:
			raise CentralError(f"{method} {route_key} -> {response.status_code}: {response.text}")
		if not response.content:
			return {}
		body = response.json()
		# Frappe wraps whitelisted return values in {"message": ...}. Unwrap so
		# callers see Central's payload directly, but tolerate a bare object too.
		if isinstance(body, dict) and "message" in body:
			message = body["message"]
			return message if isinstance(message, dict) else {"message": message}
		return body


# --- Local catalog upserts -------------------------------------------------
# Mirror atlas/atlas/doctype/provider/provider.py upsert_catalog: insert or
# update each fetched row, then disable rows Central no longer lists.


def upsert_central_sizes(sizes: tuple[CentralSizeInfo, ...]) -> dict:
	inserted = updated = 0
	seen: set[str] = set()
	for size in sizes:
		seen.add(size.slug)
		values = {
			"title": size.title,
			"vcpus": size.vcpus,
			"cpu_max_cores": size.cpu_max_cores,
			"memory_megabytes": size.memory_megabytes,
			"disk_gigabytes": size.disk_gigabytes,
			"monthly_cost_usd": size.monthly_cost_usd,
			"central_metadata": frappe.as_json(size.central_metadata or {}),
			"enabled": 1,
		}
		if frappe.db.exists("Central Size", size.slug):
			frappe.db.set_value("Central Size", size.slug, values)
			updated += 1
		else:
			frappe.get_doc({"doctype": "Central Size", "slug": size.slug, **values}).insert(
				ignore_permissions=True
			)
			inserted += 1
	disabled = _disable_missing("Central Size", seen)
	return {"inserted": inserted, "updated": updated, "disabled": disabled}


def upsert_central_images(images: tuple[CentralImageInfo, ...]) -> dict:
	inserted = updated = 0
	seen: set[str] = set()
	for image in images:
		seen.add(image.image_name)
		local_image = (
			image.image_name if frappe.db.exists("Virtual Machine Image", image.image_name) else None
		)
		values = {
			"title": image.title,
			"series": image.series,
			"central_metadata": frappe.as_json(image.central_metadata or {}),
			"local_image": local_image,
			"bake_status": _bake_status(local_image),
			"enabled": 1,
		}
		if frappe.db.exists("Central Image", image.image_name):
			frappe.db.set_value("Central Image", image.image_name, values)
			updated += 1
		else:
			frappe.get_doc({"doctype": "Central Image", "image_name": image.image_name, **values}).insert(
				ignore_permissions=True
			)
			inserted += 1
	disabled = _disable_missing("Central Image", seen)
	return {"inserted": inserted, "updated": updated, "disabled": disabled}


def _bake_status(local_image: str | None) -> str:
	"""Expected (nothing baked) vs Baked (a matching active image exists) vs
	Stale (a row exists but is no longer active)."""
	if not local_image:
		return "Expected"
	is_active = frappe.db.get_value("Virtual Machine Image", local_image, "is_active")
	return "Baked" if is_active else "Stale"


def _disable_missing(doctype: str, seen: set[str]) -> int:
	"""Set enabled=0 on rows Central no longer lists. Mirrors the disable pass
	in provider.upsert_catalog so a removed size/image stops being offered
	without deleting its history."""
	disabled = 0
	for name in frappe.get_all(doctype, filters={"enabled": 1}, pluck="name"):
		if name not in seen:
			frappe.db.set_value(doctype, name, "enabled", 0)
			disabled += 1
	return disabled
