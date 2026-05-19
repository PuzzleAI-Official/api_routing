from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from puzzle_gateway.config import settings
from puzzle_gateway.errors import ValidationError
from puzzle_gateway.models import AuditLogEntry, ProviderCredential, now_utc


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))


@dataclass(frozen=True)
class LocalEnvelopeKms:
    master_key: bytes

    @classmethod
    def from_settings(cls) -> LocalEnvelopeKms:
        seed = settings.kms_master_key.encode("utf-8")
        return cls(master_key=(seed + b"0" * 32)[:32])

    def wrap_key(self, data_key: bytes, *, aad: bytes) -> str:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.master_key).encrypt(nonce, data_key, aad)
        return _b64(nonce + ciphertext)

    def unwrap_key(self, wrapped_key: str, *, aad: bytes) -> bytes:
        payload = _unb64(wrapped_key)
        return AESGCM(self.master_key).decrypt(payload[:12], payload[12:], aad)


class Vault:
    def __init__(self, kms: LocalEnvelopeKms | None = None) -> None:
        self.kms = kms or LocalEnvelopeKms.from_settings()

    def store(
        self,
        session: Session,
        *,
        tenant_id: str,
        provider: str,
        secret: str,
        actor: str = "system",
    ) -> ProviderCredential:
        data_key = os.urandom(32)
        nonce = os.urandom(12)
        aad = f"{tenant_id}:{provider}".encode()
        ciphertext = AESGCM(data_key).encrypt(nonce, secret.encode("utf-8"), aad)
        existing = session.scalars(
            select(ProviderCredential).where(
                ProviderCredential.tenant_id == tenant_id,
                ProviderCredential.provider == provider,
                ProviderCredential.active.is_(True),
            )
        ).first()
        version = 1 if existing is None else existing.key_version + 1
        if existing is not None:
            existing.active = False
            existing.rotated_at = now_utc()
            session.add(existing)
        credential = ProviderCredential(
            tenant_id=tenant_id,
            provider=provider,
            key_version=version,
            encrypted_data_key=self.kms.wrap_key(data_key, aad=aad),
            nonce=_b64(nonce),
            ciphertext=_b64(ciphertext),
        )
        session.add(credential)
        session.flush()
        self.audit(
            session,
            tenant_id=tenant_id,
            actor=actor,
            action="credential.store",
            resource_id=credential.id,
        )
        return credential

    def retrieve(
        self,
        session: Session,
        *,
        tenant_id: str,
        credential_id: str,
        actor: str = "system",
    ) -> str:
        credential = session.get(ProviderCredential, credential_id)
        if credential is None or credential.tenant_id != tenant_id or not credential.active:
            raise ValidationError("Credential does not exist")
        aad = f"{tenant_id}:{credential.provider}".encode()
        data_key = self.kms.unwrap_key(credential.encrypted_data_key, aad=aad)
        plaintext = AESGCM(data_key).decrypt(
            _unb64(credential.nonce),
            _unb64(credential.ciphertext),
            aad,
        )
        self.audit(
            session,
            tenant_id=tenant_id,
            actor=actor,
            action="credential.retrieve",
            resource_id=credential.id,
        )
        return plaintext.decode("utf-8")

    def audit(
        self,
        session: Session,
        *,
        tenant_id: str,
        actor: str,
        action: str,
        resource_id: str,
    ) -> None:
        session.add(
            AuditLogEntry(
                tenant_id=tenant_id,
                actor=actor,
                action=action,
                resource_type="provider_credential",
                resource_id=resource_id,
                metadata_json={},
            )
        )
        session.flush()
