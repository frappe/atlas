from atlas.atlas.core.server_providers.base import ServerPowerAction


class RedfishClient:
	"""Control one Generic host through its BMC Redfish API. Not implemented yet."""

	def __init__(self, url: str, username: str, password: str) -> None:
		self.url = url
		self.username = username
		self.password = password

	def set_power_state(self, action: ServerPowerAction) -> None:
		"""Apply one power action through the `ComputerSystem.Reset` action."""
		raise NotImplementedError("Redfish power control")
