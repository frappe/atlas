from enum import StrEnum

class RescuePayloadAccess(StrEnum):
    PROTECTED = "protected"
    READ_ONLY = "read_only"
    UNPROTECTED = "unprotected"

    def __str__(self) -> str:
        return str(self.value)
