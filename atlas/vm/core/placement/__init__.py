"""Public host placement API."""

from atlas.vm.core.placement.affinity import (
	AffinityGroup,
	AffinityRule,
	AffinityRules,
	AffinityUnsatisfied,
)
from atlas.vm.core.placement.models import CurrentPlacement, PlacementRequirements
from atlas.vm.core.placement.strategies.base import (
	OutOfCapacity,
	PlacementBusy,
	PlacementStrategy,
	register,
)

__all__ = [
	"AffinityGroup",
	"AffinityRule",
	"AffinityRules",
	"AffinityUnsatisfied",
	"CurrentPlacement",
	"OutOfCapacity",
	"PlacementBusy",
	"PlacementRequirements",
	"PlacementStrategy",
	"register",
]
