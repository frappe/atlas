from enum import StrEnum

class InvestigationEntryResponseKind(StrEnum):
    ACTION = "Action"
    DECISION = "Decision"
    NOTE = "Note"
    TOOL_CALL = "Tool Call"

    def __str__(self) -> str:
        return str(self.value)
