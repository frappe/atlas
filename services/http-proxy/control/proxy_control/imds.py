import httpx

_BASE_URL = "http://169.254.169.254/latest"
_TOKEN_TTL_SECONDS = "21600"
_TOKEN_HEADERS = ("X-aws-ec2-metadata-token", "X-metadata-token")


class InstanceMetadata:
	"""Read configuration from a link-local instance metadata service.

	Read optional daemon configuration from instance user data.
	"""

	def __init__(self, base_url: str = _BASE_URL, timeout: float = 0.5) -> None:
		self._base_url = base_url
		self._timeout = timeout
		self._token_value: str | None = None

	def get_user_data(self, key: str) -> str | None:
		token = self._token_value or self._get_token()
		if token is None:
			return None
		self._token_value = token
		try:
			response = httpx.get(
				f"{self._base_url}/meta-data/user-data/{key}",
				headers=dict.fromkeys(_TOKEN_HEADERS, token),
				timeout=self._timeout,
			)
		except httpx.HTTPError:
			return None
		return response.text if response.status_code == 200 else None

	def _get_token(self) -> str | None:
		try:
			response = httpx.put(
				f"{self._base_url}/api/token",
				headers={f"{header}-ttl-seconds": _TOKEN_TTL_SECONDS for header in _TOKEN_HEADERS},
				timeout=self._timeout,
			)
		except httpx.HTTPError:
			return None
		return response.text if response.status_code == 200 else None
