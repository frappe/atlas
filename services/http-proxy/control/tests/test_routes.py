import asyncio

import pytest
from atlas_control.cluster import Mutation
from fastapi import HTTPException

from proxy_control.routes import RouteState


class _MemoryMappings:
	"""Store maps without OpenResty."""

	def __init__(self):
		self.values = {"sites": {}, "domains": {}}

	async def get(self, kind):
		return dict(self.values[kind])

	async def replace(self, kind, values):
		self.values[kind] = dict(values)
		return {"synced": True, "entries": len(values)}

	async def update(self, kind, key, address):
		self.values[kind][key] = address
		return {"site": key, "address": address}

	async def delete(self, kind, key):
		self.values[kind].pop(key, None)

	def without_reserved(self, _kind, values):
		return {key: address for key, address in values.items() if key != "proxy"}


def test_mutations_change_openresty_and_the_state():
	mappings = _MemoryMappings()
	routes = RouteState(mappings)
	state = {}

	async def run():
		await routes.apply(
			state, Mutation(kind="sites", action="replace", value={"erp": "::1", "proxy": "::2"})
		)
		await routes.apply(state, Mutation(kind="sites", action="update", key="shop", value="::3"))
		await routes.apply(state, Mutation(kind="sites", action="delete", key="erp"))

	asyncio.run(run())
	assert state == {"sites": {"shop": "::3"}}
	assert mappings.values["sites"] == {"shop": "::3"}


def test_a_mutation_with_the_wrong_value_type_changes_nothing():
	routes = RouteState(_MemoryMappings())
	state = {}

	for mutation in (
		Mutation(kind="sites", action="replace", value=["erp"]),
		Mutation(kind="sites", action="update", key="erp", value={"address": "::1"}),
	):
		with pytest.raises(HTTPException):
			asyncio.run(routes.apply(state, mutation))
	assert state == {}


def test_health_needs_openresty_to_hold_every_kind_of_route_in_the_state():
	state = {"sites": {"erp": "2001:db8::1"}, "domains": {"www.example.com": "2001:db8::2"}}

	assert RouteState.has_routes({}, {"sites": 0, "domains": 0})
	assert not RouteState.has_routes(state, {"sites": 1, "domains": 0})
	assert RouteState.has_routes(state, {"sites": 1, "domains": 1})
