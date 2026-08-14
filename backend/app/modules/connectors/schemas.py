from pydantic import BaseModel, ConfigDict

from app.modules.connectors.models import ConnectorKind, ConnectorStatus


class ConnectorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: ConnectorKind
    status: ConnectorStatus


class ConnectorAuthorizationStart(BaseModel):
    authorization_url: str
