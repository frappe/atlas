"""A tiny name-keyed callback registry so `core` can invoke post-lifecycle
behaviour it does not know about. `services` registers handlers at boot; `core`
fires an event by name and never imports `services`.

    from atlas.atlas.core import callbacks

    callbacks.register("vm.terminated", on_vm_terminated)   # services, at boot
    callbacks.run("vm.terminated", vm)                      # core, at the seam

This is the seam that keeps a core controller PaaS-blind: instead of a core
`terminate()` reaching into proxy/front-door/subdomain teardown, it fires
`vm.terminated` and services — which registered the handler — does the PaaS
work. The handlers are imported at app boot (`hooks.py`) so the registry is
populated before any lifecycle fires.

Two dispatch shapes, both faithful to inlining the handlers (exceptions
propagate, so replacing a direct core→PaaS call with `run` preserves its error
behaviour exactly; a handler that wants per-step resilience isolates internally,
as the teardown/reconcile handlers already do):
  - `run(event, …)` — invoke every handler for a side effect (teardown, route
    re-point) and return the list of results.
  - `run_first(event, …)` — the first non-None handler result, for a predicate
    or lookup hook (e.g. "is this VM's status owned by a front door?").
"""

from __future__ import annotations

from collections.abc import Callable

import frappe

_REGISTRY: dict[str, list[Callable]] = {}
_loaded = False


def _ensure_loaded() -> None:
	"""Import the services registration modules named by the `services_callbacks`
	hook, once. Discovering them through `frappe.get_hooks` (not a literal import)
	is what lets core populate its registry without naming `services` in its own
	source — the one-way import rule stays intact. Each named module self-registers
	its handlers at import time."""
	global _loaded
	if _loaded:
		return
	_loaded = True
	for module_path in frappe.get_hooks("services_callbacks") or []:
		frappe.get_module(module_path)


def register(event: str, handler: Callable) -> None:
	"""Register `handler` for `event`. Idempotent per (event, handler) so a
	re-imported services module never double-registers."""
	handlers = _REGISTRY.setdefault(event, [])
	if handler not in handlers:
		handlers.append(handler)


def run(event: str, *args, **kwargs) -> list:
	"""Invoke every handler for `event`, in registration order, and return the
	list of results. Exceptions propagate (faithful to a direct call): a handler
	wanting per-step resilience isolates internally."""
	_ensure_loaded()
	return [handler(*args, **kwargs) for handler in _REGISTRY.get(event, [])]


def run_first(event: str, *args, **kwargs):
	"""The first non-None result from `event`'s handlers (a predicate/lookup
	hook), or None if there are none. Exceptions propagate — a predicate that
	cannot answer must fail loud, unlike a fire-and-forget side effect."""
	_ensure_loaded()
	for handler in _REGISTRY.get(event, []):
		result = handler(*args, **kwargs)
		if result is not None:
			return result
	return None


def registered(event: str) -> bool:
	"""Whether any handler is registered for `event` — lets a core caller keep
	an exact no-op when nothing is registered (e.g. a core-only test bench with
	services handlers not loaded)."""
	_ensure_loaded()
	return bool(_REGISTRY.get(event))
