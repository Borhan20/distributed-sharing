"""TOTP codes (RFC 6238, HMAC-SHA1, 6 digits, 30 s): what an authenticator app on a phone computes.

Used by the tests to complete two-step sign-in in Keycloak like a person would.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time


def code(secret_base32: str, at: float | None = None, digits: int = 6, period: int = 30) -> str:
    key = base64.b32decode(secret_base32.replace(" ", "").upper() + "=" * (-len(secret_base32.replace(" ", "")) % 8))
    counter = int((time.time() if at is None else at) // period)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits)


if __name__ == "__main__":
    # RFC 6238 appendix B test vector (SHA1, T=59 s, 8 digits): 94287082
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert code(secret, at=59, digits=8) == "94287082", code(secret, at=59, digits=8)
    print("RFC 6238 test vector ok")
