import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from google.cloud import kms


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    ciphertext: bytes
    encrypted_data_key: bytes
    nonce: bytes
    algorithm: str
    key_version: str


class KeyWrapper(Protocol):
    async def wrap(self, data_key: bytes) -> tuple[bytes, str]: ...

    async def unwrap(self, encrypted_data_key: bytes, key_version: str) -> bytes: ...


class FileKeyWrapper:
    """Development/self-hosted key wrapper; never selected implicitly in production."""

    _KEY_VERSION = "file-v1"

    def __init__(
        self,
        key_path: Path,
        *,
        app_env: str = "development",
        self_hosted_file_key_allowed: bool = False,
    ) -> None:
        if app_env != "development" and not self_hosted_file_key_allowed:
            raise ValueError(
                "FileKeyWrapper is only allowed in development or approved self-hosted deployments"
            )
        self._key_path = key_path

    def _master_key(self) -> bytes:
        try:
            key = self._key_path.read_bytes()
        except FileNotFoundError as exc:
            raise RuntimeError("CONNECTOR_FILE_KEY_PATH must point to a 32-byte key") from exc
        if len(key) != 32:
            raise ValueError("connector file key must be exactly 32 bytes")
        return key

    async def wrap(self, data_key: bytes) -> tuple[bytes, str]:
        nonce = os.urandom(12)
        return nonce + AESGCM(self._master_key()).encrypt(nonce, data_key, None), self._KEY_VERSION

    async def unwrap(self, encrypted_data_key: bytes, key_version: str) -> bytes:
        if key_version != self._KEY_VERSION:
            raise ValueError("unsupported file key version")
        return AESGCM(self._master_key()).decrypt(
            encrypted_data_key[:12], encrypted_data_key[12:], None
        )


class GoogleCloudKmsKeyWrapper:
    def __init__(self, key_name: str) -> None:
        self._key_name = key_name
        self._client = kms.KeyManagementServiceAsyncClient()

    async def wrap(self, data_key: bytes) -> tuple[bytes, str]:
        response = await self._client.encrypt(
            request={"name": self._key_name, "plaintext": data_key}
        )
        return bytes(response.ciphertext), self._key_name

    async def unwrap(self, encrypted_data_key: bytes, key_version: str) -> bytes:
        response = await self._client.decrypt(
            request={"name": key_version, "ciphertext": encrypted_data_key}
        )
        return bytes(response.plaintext)


class EnvelopeCipher:
    def __init__(self, key_wrapper: KeyWrapper) -> None:
        self._key_wrapper = key_wrapper

    async def encrypt(self, plaintext: str) -> EncryptedSecret:
        data_key = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        ciphertext = AESGCM(data_key).encrypt(nonce, plaintext.encode("utf-8"), None)
        encrypted_data_key, key_version = await self._key_wrapper.wrap(data_key)
        return EncryptedSecret(
            ciphertext=ciphertext,
            encrypted_data_key=encrypted_data_key,
            nonce=nonce,
            algorithm="AES-256-GCM",
            key_version=key_version,
        )

    async def decrypt(self, secret: EncryptedSecret) -> str:
        if secret.algorithm != "AES-256-GCM":
            raise ValueError("unsupported connector secret algorithm")
        data_key = await self._key_wrapper.unwrap(secret.encrypted_data_key, secret.key_version)
        return AESGCM(data_key).decrypt(secret.nonce, secret.ciphertext, None).decode("utf-8")
