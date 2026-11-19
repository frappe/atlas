from enum import StrEnum

class DnsRecordPayloadTypeType0(StrEnum):
    A = "A"
    AAAA = "AAAA"
    TXT = "TXT"

    def __str__(self) -> str:
        return str(self.value)
