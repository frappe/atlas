from enum import StrEnum

class RescuePayloadAction(StrEnum):
    FINISH = "finish"
    ROLLBACK = "rollback"
    VALUE_0 = ""

    def __str__(self) -> str:
        return str(self.value)
