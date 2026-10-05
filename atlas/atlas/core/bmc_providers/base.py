from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from atlas.atlas.core.server_providers.base import ProviderOperationError, ServerPowerAction


class BMCError(ProviderOperationError):
	"""Report a baseboard management controller failure."""


@dataclass(frozen=True, slots=True)
class BMCPowerStatus:
	"""Report the power state and health that a BMC observes."""

	power_state: str
	health: str | None


class BMCProvider(ABC):
	"""Control one machine through its baseboard management controller (BMC)."""

	def __init__(self, url: str, username: str = "", password: str = "") -> None:
		if bool(username) != bool(password):
			raise BMCError("Provide both the BMC username and password, or leave both empty")
		self.url = url

	@abstractmethod
	def read_power_status(self) -> BMCPowerStatus:
		"""Read the current power state and health."""
		...

	@abstractmethod
	def set_power_state(self, action: ServerPowerAction) -> None:
		"""Apply one power action."""
		...
