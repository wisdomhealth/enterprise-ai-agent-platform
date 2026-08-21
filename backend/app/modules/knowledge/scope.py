from dataclasses import dataclass

from app.modules.knowledge.drive_gateway import DriveFile


@dataclass(frozen=True, slots=True)
class DriveScope:
    root_folder_id: str
    allowed_descendant_ids: set[str]

    def is_authorized(self, file: DriveFile) -> bool:
        if file.removed:
            return False
        permitted_parent_ids = self.allowed_descendant_ids | {self.root_folder_id}
        return bool(permitted_parent_ids.intersection(file.parent_ids))
