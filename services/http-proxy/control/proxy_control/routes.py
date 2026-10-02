from typing import Any

from atlas_control.cluster import Mutation
from fastapi import HTTPException

from .mappings import MappingStore

ROUTE_KINDS = ("sites", "domains")


class RouteState:
	"""Replicate the site and domain maps that OpenResty serves."""

	def __init__(self, mappings: MappingStore):
		self.mappings = mappings

	async def initial_state(self) -> dict[str, Any]:
		return {kind: await self.mappings.get(kind) for kind in ROUTE_KINDS}

	def is_serving(self) -> bool:
		# The OpenResty health check owns traffic health. The proxy places nothing on a member.
		return True

	async def prepare(self, state: dict[str, Any], mutation: Mutation, serving_members: frozenset[str]) -> Mutation:
		return mutation

	async def restore(self, state: dict[str, Any]) -> None:
		for kind in ROUTE_KINDS:
			await self.mappings.replace(kind, state.get(kind, {}))

	async def apply(self, state: dict[str, Any], mutation: Mutation) -> dict[str, object]:
		if mutation.action == "replace":
			if not _is_map(mutation.value):
				raise HTTPException(status_code=400, detail="a map replacement needs string values")
			values = self.mappings.without_reserved(mutation.kind, mutation.value)
			body = await self.mappings.replace(mutation.kind, values)
			state[mutation.kind] = dict(values)
			return body

		if mutation.action == "update":
			if not isinstance(mutation.value, str):
				raise HTTPException(status_code=400, detail="a route update needs one address")
			body = await self.mappings.update(mutation.kind, mutation.key, mutation.value)
			state.setdefault(mutation.kind, {})[mutation.key] = mutation.value
			return body

		await self.mappings.delete(mutation.kind, mutation.key)
		state.setdefault(mutation.kind, {}).pop(mutation.key, None)
		return {}

	@staticmethod
	def has_routes(state: dict[str, Any], counts: dict[str, object]) -> bool:
		"""Report whether OpenResty holds the routes of this state."""
		return all(not state.get(kind) or int(counts.get(kind, 0)) > 0 for kind in ROUTE_KINDS)


def _is_map(value: object) -> bool:
	return isinstance(value, dict) and all(
		isinstance(key, str) and isinstance(address, str) for key, address in value.items()
	)
