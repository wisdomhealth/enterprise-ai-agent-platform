from app.modules.knowledge.drive_gateway import DriveGateway


def test_drive_gateway_declares_read_only_scope() -> None:
    assert DriveGateway.oauth_scopes == ("https://www.googleapis.com/auth/drive.readonly",)


def test_drive_gateway_exposes_no_mutating_drive_methods() -> None:
    gateway_methods = set(dir(DriveGateway))

    assert {"create", "update", "move", "delete"}.isdisjoint(gateway_methods)
