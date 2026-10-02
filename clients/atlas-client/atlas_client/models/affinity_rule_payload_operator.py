from enum import StrEnum

class AffinityRulePayloadOperator(StrEnum):
    HAS = "has"
    HAS_NOT = "has_not"

    def __str__(self) -> str:
        return str(self.value)
