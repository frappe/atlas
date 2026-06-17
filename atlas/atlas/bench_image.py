"""Golden bench image control plane — bake a bench-preinstalled image by building
INSIDE a plain guest over SSH, then snapshotting it.

This is the controller side of the golden bench image (spec/08-images.md). The
build itself — upload the committed `bench/` tree, run `build.sh` over guest-SSH
detached, record a Task, fail loud — is the shared `image_builder.run_build` seam;
`build_bench` is the thin wrapper that hands it the `bench` recipe
(`image_recipes.RECIPES["bench"]`). The full provision→build→stop→snapshot→register
lifecycle around it lives in the `Image Build` DocType.

That snapshot is the reusable "golden bench image" — a VM with bench-cli, the uv
venv, the Frappe clone, MariaDB + Redis, AND a fully-created site baked under the
fixed name `site.local`, so `deploy-site.py` (spec/14-self-serve.md) only RENAMES
that baked site to the per-VM FQDN (a directory move) + regenerates the nginx vhost
(`bench setup nginx`), never paying the multi-minute `bench new-site` per signup.
"""

from atlas.atlas.image_builder import run_build
from atlas.atlas.image_recipes import get_recipe


def build_bench(virtual_machine: str) -> None:
	"""Turn a freshly-provisioned Ubuntu guest into a golden bench: upload the
	committed `bench/` tree and run build.sh inside the guest (install bench-cli +
	`bench init` + bake a `site.local` site). After this returns the caller stops +
	snapshots the VM; that snapshot is the rollable golden image.

	Idempotent (build.sh re-runs cleanly), so this doubles as the "re-bake" verb.
	Recorded as a `bench-build` Task row for the audit trail, like every guest op."""
	run_build(virtual_machine, get_recipe("bench"))
