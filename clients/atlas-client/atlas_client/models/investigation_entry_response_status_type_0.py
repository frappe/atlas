from enum import StrEnum

class InvestigationEntryResponseStatusType0(StrEnum):
    FAILED = "Failed"
    REFUSED = "Refused"
    SUCCESS = "Success"

    def __str__(self) -> str:
        return str(self.value)
