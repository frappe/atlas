import importlib
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from atlas_control.cluster import ClusterSnapshot
from atlas_control.config import ClusterPeer
from fastapi.testclient import TestClient


def _module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
	"""Import the app with a configuration file the test controls."""
	path = tmp_path / "proxy-control.toml"
	path.write_text(
		"""[control]
domain = "proxy-001.par-1.example.com"

[tls]
wildcard_domain = "*.par-1.example.com"
fullchain_pem = "leaf"
private_key_pem = "key"
"""
	)
	monkeypatch.setenv("ATLAS_PROXY_CONTROL_CONFIG", str(path))
	sys.modules.pop("proxy_control.main", None)
	return importlib.import_module("proxy_control.main")


def test_health_fails_before_the_snapshot_is_applied(tmp_path, monkeypatch):
	main = _module(tmp_path, monkeypatch)
	main.cluster.is_initialized = False

	assert TestClient(main.app).get("/healthz").status_code == 503


def test_health_fails_when_openresty_is_unreachable(tmp_path, monkeypatch):
	main = _module(tmp_path, monkeypatch)
	main.cluster.is_initialized = True
	main.proxy.request = AsyncMock(side_effect=httpx.ConnectError("down"))

	assert TestClient(main.app).get("/healthz").status_code == 503


def test_health_fails_when_openresty_lost_the_routes(tmp_path, monkeypatch):
	main = _module(tmp_path, monkeypatch)
	main.cluster.is_initialized = True
	main.cluster.snapshot = ClusterSnapshot(state={"sites": {"erp": "2001:db8::1"}})
	main.proxy.request = AsyncMock(return_value=(200, {"sites": 0, "domains": 0}))

	assert TestClient(main.app).get("/healthz").status_code == 503


def test_health_passes_when_openresty_holds_the_routes(tmp_path, monkeypatch):
	main = _module(tmp_path, monkeypatch)
	main.cluster.is_initialized = True
	main.cluster.snapshot = ClusterSnapshot(state={"sites": {"erp": "2001:db8::1"}})
	main.proxy.request = AsyncMock(return_value=(200, {"sites": 1, "domains": 0}))

	assert TestClient(main.app).get("/healthz").status_code == 204


def test_a_node_without_a_leader_serves_traffic_but_is_not_ready(tmp_path, monkeypatch):
	"""Lost quorum stops control tasks. It does not remove the node from DNS."""
	main = _module(tmp_path, monkeypatch)
	main.cluster.configuration = replace(
		main.cluster.configuration,
		node_id="proxy-001",
		password="secret",
		peers=(
			ClusterPeer(node_id="proxy-001", address="https://proxy-001.example.com"),
			ClusterPeer(node_id="proxy-002", address="https://proxy-002.example.com"),
			ClusterPeer(node_id="proxy-003", address="https://proxy-003.example.com"),
		),
	)
	main.cluster.is_initialized = True
	main.cluster.is_synchronized = True
	main.cluster.leader_id = ""
	main.cluster.snapshot = ClusterSnapshot(state={"sites": {"erp": "2001:db8::1"}})
	main.proxy.request = AsyncMock(return_value=(200, {"sites": 1, "domains": 0}))

	client = TestClient(main.app)

	assert client.get("/healthz").status_code == 204
	assert client.get("/readyz").status_code == 503
