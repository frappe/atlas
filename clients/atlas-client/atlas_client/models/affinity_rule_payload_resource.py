from enum import StrEnum

class AffinityRulePayloadResource(StrEnum):
    METAL_SERVER = "metal_server"
    VIRTUAL_MACHINE = "virtual_machine"

    def __str__(self) -> str:
        return str(self.value)
