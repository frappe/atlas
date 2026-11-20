from enum import StrEnum

class DnsRecordPayloadType(StrEnum):
    A = "A"
    AAAA = "AAAA"
    CNAME = "CNAME"
    TXT = "TXT"

    def __str__(self) -> str:
        return str(self.value)
