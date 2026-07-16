"""App install / migrate hooks.

Wired in hooks.py. `after_migrate` runs on every `bench migrate`. The brand denylist it
used to seed (spec/18 Component H) moved to the Satellite orchestrator with the rest of
the guest routing plane (spec/28), so there is nothing to seed here anymore — the hook
is kept as the idempotent seam for any future provisioner-owned seed.
"""


def after_migrate() -> None:
	"""No-op. The Subdomain Denylist (and the whole guest self-service routing plane)
	now lives on the Satellite; Satellite seeds its own denylist on migrate."""
