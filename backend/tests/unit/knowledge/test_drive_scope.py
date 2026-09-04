from datetime import UTC, datetime

from app.modules.knowledge.drive_gateway import DriveFile
from app.modules.knowledge.scope import DriveScope


def test_file_outside_authorized_tree_is_rejected() -> None:
    scope = DriveScope(root_folder_id="allowed", allowed_descendant_ids={"child"})
    file = DriveFile(
        id="file-1",
        name="Private.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        modified_time=datetime.now(UTC),
        parent_ids=("private",),
        web_view_link="https://drive.example/private",
        removed=False,
    )

    assert scope.is_authorized(file) is False


def test_file_in_authorized_descendant_is_accepted() -> None:
    scope = DriveScope(root_folder_id="allowed", allowed_descendant_ids={"child"})
    file = DriveFile(
        id="file-2",
        name="Shared.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        modified_time=datetime.now(UTC),
        parent_ids=("child",),
        web_view_link="https://drive.example/shared",
        removed=False,
    )

    assert scope.is_authorized(file) is True


def test_removed_file_is_not_authorized_for_download() -> None:
    scope = DriveScope(root_folder_id="allowed", allowed_descendant_ids=set())
    file = DriveFile(
        id="file-3",
        name="Deleted.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        modified_time=datetime.now(UTC),
        parent_ids=("allowed",),
        web_view_link="https://drive.example/deleted",
        removed=True,
    )

    assert scope.is_authorized(file) is False
