"""Authenticated encryption for server-only integration credentials."""

import base64
from functools import lru_cache

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from open_webui.env import WEBUI_SECRET_KEY
from open_webui.utils.json_codec import JSONCodec


@lru_cache(maxsize=4)
def cipher(purpose: str) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=f'open-webui/team/{purpose}/v1'.encode()).derive(
        WEBUI_SECRET_KEY.encode()
    )
    return Fernet(base64.urlsafe_b64encode(key))


def seal(value: dict, purpose: str) -> str:
    return cipher(purpose).encrypt(JSONCodec.dumps(value).encode()).decode()


def unseal(value: str, purpose: str) -> dict:
    # A key/configuration error must surface; never silently discard stored credentials.
    return JSONCodec.loads(cipher(purpose).decrypt(value.encode()))
