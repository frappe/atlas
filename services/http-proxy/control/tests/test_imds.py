import httpx

from proxy_control.imds import InstanceMetadata


class _Response:
	def __init__(self, status_code: int, text: str = ""):
		self.status_code = status_code
		self.text = text


def test_get_user_data_returns_value(monkeypatch):
	monkeypatch.setattr(httpx, "put", lambda *a, **k: _Response(200, "token-abc"))
	monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response(200, "https://issuer.example.com/jwks.json"))

	value = InstanceMetadata().get_user_data("proxy_jwks_url")

	assert value == "https://issuer.example.com/jwks.json"


def test_get_user_data_sends_both_token_header_names(monkeypatch):
	requests = {}

	def fake_put(url, headers, **kwargs):
		requests["put"] = headers
		return _Response(200, "token-abc")

	def fake_get(url, headers, **kwargs):
		requests["get"] = headers
		return _Response(200, "value")

	monkeypatch.setattr(httpx, "put", fake_put)
	monkeypatch.setattr(httpx, "get", fake_get)

	InstanceMetadata().get_user_data("proxy_jwks_url")

	assert requests["put"] == {
		"X-aws-ec2-metadata-token-ttl-seconds": "21600",
		"X-metadata-token-ttl-seconds": "21600",
	}
	assert requests["get"] == {
		"X-aws-ec2-metadata-token": "token-abc",
		"X-metadata-token": "token-abc",
	}


def test_get_user_data_returns_none_when_token_fetch_fails(monkeypatch):
	monkeypatch.setattr(httpx, "put", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("no route")))

	assert InstanceMetadata().get_user_data("proxy_jwks_url") is None


def test_get_user_data_returns_none_when_key_is_unset(monkeypatch):
	monkeypatch.setattr(httpx, "put", lambda *a, **k: _Response(200, "token-abc"))
	monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response(404))

	assert InstanceMetadata().get_user_data("proxy_jwks_audience_id") is None


def test_get_user_data_returns_none_on_network_error(monkeypatch):
	monkeypatch.setattr(httpx, "put", lambda *a, **k: _Response(200, "token-abc"))
	monkeypatch.setattr(
		httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectTimeout("timed out"))
	)

	assert InstanceMetadata().get_user_data("proxy_jwks_url") is None


def test_token_is_fetched_once_and_reused(monkeypatch):
	calls = []
	monkeypatch.setattr(httpx, "put", lambda *a, **k: calls.append(1) or _Response(200, "token-abc"))
	monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response(200, "value"))

	metadata = InstanceMetadata()
	metadata.get_user_data("proxy_jwks_url")
	metadata.get_user_data("proxy_jwks_audience_id")

	assert len(calls) == 1
