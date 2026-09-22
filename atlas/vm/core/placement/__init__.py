"""Public host placement API."""

from atlas.vm.core.placement.models import CurrentPlacement, PlacementRequirements
from atlas.vm.core.placement.strategies.base import (
	OutOfCapacity,
	PlacementBusy,
	PlacementStrategy,
	register,
)

__all__ = [
	"CurrentPlacement",
	"OutOfCapacity",
	"PlacementBusy",
	"PlacementRequirements",
	"PlacementStrategy",
	"register",
]
