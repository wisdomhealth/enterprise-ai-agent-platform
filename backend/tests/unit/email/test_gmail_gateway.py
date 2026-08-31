from types import SimpleNamespace

import pytest

from app.modules.email.gmail_gateway import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SCOPES,
    GMAIL_SEND_SCOPE,
    GoogleGmailGatewayFactory,
    GoogleGmailReadClient,
)


class _Request:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def execute(self) -> dict[str, object]:
        return self._payload


class _BootstrapMessages:
    def __init__(self, profile_call_count) -> None:  # type: ignore[no-untyped-def]
        self._profile_call_count = profile_call_count
        self.page_tokens: list[str | None] = []

    def list(self, **kwargs: object) -> _Request:
        page_token = kwargs.get("pageToken")
        assert page_token is None or isinstance(page_token, str)
        assert self._profile_call_count() == 1
        self.page_tokens.append(page_token)
        if page_token is None:
            return _Request(
                {"messages": [{"id": "m-1"}], "nextPageToken": "provider-page-2"}
            )
        return _Request({"messages": [{"id": "m-2"}]})


class _BootstrapUsers:
    def __init__(self) -> None:
        self.profile_calls = 0
        self.history_calls = 0
        self.messages_api = _BootstrapMessages(lambda: self.profile_calls)

    def getProfile(self, **_kwargs: object) -> _Request:  # noqa: N802
        self.profile_calls += 1
        return _Request({"historyId": "anchor-101"})

    def messages(self) -> _BootstrapMessages:
        return self.messages_api

    def history(self) -> "_BootstrapUsers":
        self.history_calls += 1
        return self

    def list(self, **_kwargs: object) -> _Request:
        raise AssertionError("bootstrap pagination must not switch to Gmail history.list")


@pytest.mark.asyncio
async def test_bootstrap_pagination_reuses_the_first_page_history_anchor() -> None:
    users = _BootstrapUsers()
    client = GoogleGmailReadClient(SimpleNamespace(users=lambda: users))

    first = await client.list_history(None)
    second = await client.list_history(first.history_id, first.next_page_token)

    assert first.message_ids == ("m-1",)
    assert second.message_ids == ("m-2",)
    assert second.history_id == "anchor-101"
    assert users.profile_calls == 1
    assert users.history_calls == 0
    assert users.messages_api.page_tokens == [None, "provider-page-2"]


@pytest.mark.asyncio
async def test_gmail_factory_uses_only_the_approved_connector_scopes(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_credentials(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("app.modules.email.gmail_gateway.Credentials", fake_credentials)
    monkeypatch.setattr(
        "app.modules.email.gmail_gateway.build",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )

    connection = await GoogleGmailGatewayFactory(
        client_id="gmail-client", client_secret="gmail-secret"
    ).create(refresh_token="encrypted-boundary-output")

    assert connection.gateway.oauth_scopes == GMAIL_SCOPES
    assert captured["scopes"] == [GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE]
    assert captured["refresh_token"] == "encrypted-boundary-output"
