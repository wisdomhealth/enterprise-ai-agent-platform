from pathlib import Path

import pytest

from app.modules.connectors.encryption import EnvelopeCipher, FileKeyWrapper


@pytest.fixture
def cipher(tmp_path: Path) -> EnvelopeCipher:
    key_path = tmp_path / "connector-master-key"
    key_path.write_bytes(b"a" * 32)
    return EnvelopeCipher(FileKeyWrapper(key_path))


@pytest.mark.asyncio
async def test_envelope_cipher_round_trip_uses_unique_data_keys(cipher: EnvelopeCipher) -> None:
    first = await cipher.encrypt("refresh-token")
    second = await cipher.encrypt("refresh-token")

    assert first.ciphertext != second.ciphertext
    assert first.encrypted_data_key != second.encrypted_data_key
    assert await cipher.decrypt(first) == "refresh-token"


def test_file_key_wrapper_is_rejected_in_production(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="development"):
        FileKeyWrapper(tmp_path / "connector-master-key", app_env="production")
