"""Guest-side tests for `bench/deploy-site.py` — the per-VM front-door script run
INSIDE the golden bench VM over guest-SSH. Stdlib-only, no host: the script's file
edits (`_set_admin_domain` rewriting bench.toml's `[admin].domain`, and its
`_update_pilot_endpoint` re-pointing a site's `pilot_endpoint`) are pure disk ops,
so we load the script by path and exercise them against a tmp bench tree.

The `pilot_endpoint` key is baked into `site_config.json` at new-site time (pilot
new_site.py) as the `admin.localhost` placeholder — the real admin domain isn't
known then. Site-mode deploy learns the admin FQDN and must re-point it, so a
deployed site calls Pilot back at its real admin console, not `admin.localhost`.
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_SITE = _REPO_ROOT / "bench" / "deploy-site.py"


def _load_by_path(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	# Register before exec: the script's @dataclass decorators resolve annotations
	# via sys.modules[cls.__module__], which is None until the module is registered.
	sys.modules[name] = module
	spec.loader.exec_module(module)
	return module


class UpdatePilotEndpointTest(unittest.TestCase):
	def setUp(self) -> None:
		# Load the guest script and repoint its baked absolute paths (SITES_DIR,
		# BENCH_TOML) at a tmp bench tree so the file edits land under the test dir.
		self.mod = _load_by_path("deploy_site_guest", _DEPLOY_SITE)
		self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
		self.sites = self.tmp / "sites"
		self.sites.mkdir()
		self.mod.SITES_DIR = str(self.sites)
		self.mod.BENCH_TOML = str(self.tmp / "bench.toml")

	def _make_site(self, name: str, endpoint: str = "http://admin.localhost") -> Path:
		site_dir = self.sites / name
		site_dir.mkdir()
		config = site_dir / "site_config.json"
		config.write_text(json.dumps({"db_name": "x", "pilot_endpoint": endpoint}))
		return config

	def test_repoints_placeholder_endpoint_at_admin_fqdn(self) -> None:
		config = self._make_site(self.mod.BAKED_SITE)
		self.mod._update_pilot_endpoint(self.mod.BAKED_SITE, "acme.blr1.frappe.dev")
		data = json.loads(config.read_text())
		self.assertEqual(data["pilot_endpoint"], "https://acme.blr1.frappe.dev")
		self.assertEqual(data["db_name"], "x")  # other keys untouched

	def test_is_idempotent(self) -> None:
		config = self._make_site(self.mod.BAKED_SITE)
		self.mod._update_pilot_endpoint(self.mod.BAKED_SITE, "acme.blr1.frappe.dev")
		self.mod._update_pilot_endpoint(self.mod.BAKED_SITE, "acme.blr1.frappe.dev")
		data = json.loads(config.read_text())
		self.assertEqual(data["pilot_endpoint"], "https://acme.blr1.frappe.dev")

	def test_missing_config_is_a_noop(self) -> None:
		# A re-run after the rename targets the baked dir, which no longer exists —
		# must not raise (the FQDN dir already carries the corrected value).
		self.mod._update_pilot_endpoint(self.mod.BAKED_SITE, "acme.blr1.frappe.dev")


class SetAdminDomainWritesEndpointTest(unittest.TestCase):
	def setUp(self) -> None:
		self.mod = _load_by_path("deploy_site_guest", _DEPLOY_SITE)
		self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
		self.sites = self.tmp / "sites"
		self.sites.mkdir()
		self.bench_toml = self.tmp / "bench.toml"
		self.bench_toml.write_text('[admin]\ndomain = "admin.localhost"\n')
		self.mod.SITES_DIR = str(self.sites)
		self.mod.BENCH_TOML = str(self.bench_toml)

	def test_update_site_repoints_endpoint_alongside_admin_domain(self) -> None:
		site_dir = self.sites / self.mod.BAKED_SITE
		site_dir.mkdir()
		config = site_dir / "site_config.json"
		config.write_text(json.dumps({"pilot_endpoint": "http://admin.localhost"}))

		self.mod._set_admin_domain("acme.blr1.frappe.dev", run_setup=False, update_site=self.mod.BAKED_SITE)

		self.assertIn('domain = "acme.blr1.frappe.dev"', self.bench_toml.read_text())
		data = json.loads(config.read_text())
		self.assertEqual(data["pilot_endpoint"], "https://acme.blr1.frappe.dev")

	def test_no_update_site_leaves_configs_alone(self) -> None:
		# admin mode (no baked site) passes no update_site — only bench.toml changes.
		self.mod._set_admin_domain("acme.blr1.frappe.dev", run_setup=False)
		self.assertIn('domain = "acme.blr1.frappe.dev"', self.bench_toml.read_text())


class ReissuePilotAuthTokenTest(unittest.TestCase):
	"""The baked `pilot_auth_token` is a JWT scoped (via its `site` claim) to the
	placeholder `site.local`; after the rename to the FQDN the bench rejects it, so
	site-mode deploy re-issues one scoped to the FQDN. `_bench` shells out to the
	guest bench-cli, so stub it to return a fixed token and assert we scope + write it.
	"""

	def setUp(self) -> None:
		self.mod = _load_by_path("deploy_site_guest", _DEPLOY_SITE)
		self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
		self.sites = self.tmp / "sites"
		self.sites.mkdir()
		self.mod.SITES_DIR = str(self.sites)
		self.calls: list[tuple] = []

		def fake_bench(*args, capture=False):
			self.calls.append(args)
			return "jwt.for.fqdn\n"

		self.mod._bench = fake_bench

	def _make_site(self, name: str) -> Path:
		site_dir = self.sites / name
		site_dir.mkdir()
		config = site_dir / "site_config.json"
		config.write_text(json.dumps({"db_name": "x", "pilot_auth_token": "jwt.for.site.local"}))
		return config

	def test_reissues_token_scoped_to_fqdn(self) -> None:
		config = self._make_site("acme.blr1.frappe.dev")
		self.mod._reissue_pilot_auth_token("acme.blr1.frappe.dev")
		self.assertEqual(
			self.calls,
			[("issue-site-token", "acme.blr1.frappe.dev", "--ttl", str(365 * 24 * 3600))],
		)
		data = json.loads(config.read_text())
		self.assertEqual(data["pilot_auth_token"], "jwt.for.fqdn")
		self.assertEqual(data["db_name"], "x")  # other keys untouched

	def test_missing_config_is_a_noop(self) -> None:
		# The FQDN dir doesn't exist yet — must not raise and must not shell out.
		self.mod._reissue_pilot_auth_token("acme.blr1.frappe.dev")
		self.assertEqual(self.calls, [])


if __name__ == "__main__":
	unittest.main()
