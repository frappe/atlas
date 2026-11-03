from enum import StrEnum

class ImageResponseStatus(StrEnum):
    ARCHIVED = "archived"
    AVAILABLE = "available"
    BUILDING = "building"
    CLEANING = "cleaning"
    COMPLETING = "completing"
    DELETING = "deleting"
    FAILED = "failed"
    PENDING = "pending"
    SNAPSHOTTING = "snapshotting"
    UPLOADING = "uploading"

    def __str__(self) -> str:
        return str(self.value)
