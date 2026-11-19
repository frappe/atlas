from enum import StrEnum

class DnsRecordPayloadTypeType0(StrEnum):
    A = "A"
    AAAA = "AAAA"
    CNAME = "CNAME"
    TXT = "TXT"

    def __str__(self) -> str:
        return str(self.value)
