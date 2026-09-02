from typing import Any

import httpx


class ProxyClient:
	"""Send asynchronous HTTP requests to OpenResty through its Unix socket."""

	def __init__(self, socket_path: str):
		self.client = httpx.AsyncClient(
			transport=httpx.AsyncHTTPTransport(uds=socket_path),
			base_url="http://localhost",
			timeout=5,
		)

	async def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
		response = await self.client.request(method, path, json=body)
		if not response.content:
			return response.status_code, None
		try:
			return response.status_code, response.json()
		except ValueError:
			return response.status_code, response.text

	async def close(self) -> None:
		await self.client.aclose()
