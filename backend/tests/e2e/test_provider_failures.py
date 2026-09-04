import httpx
import pytest
from fakes.providers import DeterministicProviderStack, DriveFixture, HttpEmbeddingProvider


@pytest.mark.asyncio
async def test_fake_drive_preserves_pagination_and_authorization_removal() -> None:
    providers = DeterministicProviderStack()
    providers.add_drive_file(DriveFixture("policy", "Policy.pdf", "application/pdf", b"approved"))
    providers.state.drive_pages = {
        "drive-start-1": (["policy"], "drive-page-2"),
        "drive-page-2": (["policy"], None),
    }
    async with providers.client("drive") as drive:
        first = await drive.get("/drive/v3/changes", params={"pageToken": "drive-start-1"})
        assert first.json()["nextPageToken"] == "drive-page-2"
        providers.revoke_drive_file("policy")
        second = await drive.get("/drive/v3/changes", params={"pageToken": "drive-page-2"})
        assert second.json()["changes"][0]["removed"] is True
        denied = await drive.get("/drive/v3/files/policy", params={"alt": "media"})
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_fake_gmail_send_then_timeout_is_searchable_and_idempotent() -> None:
    providers = DeterministicProviderStack()
    async with providers.client("gmail") as gmail:
        with pytest.raises(httpx.ReadTimeout):
            await gmail.post(
                "/gmail/v1/users/me/messages/send",
                json={"raw": "same-message", "threadId": "thread-1"},
                headers={"X-Fake-Send-Then-Timeout": "1"},
            )
        replay = await gmail.post(
            "/gmail/v1/users/me/messages/send",
            json={"raw": "same-message", "threadId": "thread-1"},
        )
        assert replay.status_code == 200
        assert len(providers.state.gmail_sent) == 1


@pytest.mark.asyncio
async def test_fake_embeddings_are_deterministic_and_provider_failures_are_explicit() -> None:
    providers = DeterministicProviderStack()
    async with providers.client("openai") as client:
        adapter = HttpEmbeddingProvider(client)
        assert await adapter.embed(["policy"]) == await adapter.embed(["policy"])
        providers.fail_once("openai", "rate_limited")
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.embed(["policy"])
