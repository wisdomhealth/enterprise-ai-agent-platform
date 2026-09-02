from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict

from app.modules.connectors.models import ConnectorKind, ConnectorStatus

GOOGLE_CONNECTOR_SCOPES: Final = MappingProxyType(
    {
        ConnectorKind.DRIVE: ("https://www.googleapis.com/auth/drive.readonly",),
        ConnectorKind.GMAIL: (
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
        ),
    }
)


class ConnectorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: ConnectorKind
    status: ConnectorStatus


class ConnectorAuthorizationStart(BaseModel):
    authorization_url: str
