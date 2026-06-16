from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.request
from typing import Any, Iterable


class ClerkAuthError(RuntimeError):
    pass


_JWKS_CACHE_SECONDS = 300
_SHA256_DIGESTINFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_lock = threading.Lock()


def verify_clerk_token(
    token: str,
    *,
    jwks_json: str | None = None,
    jwks_url: str | None = None,
    jwks_bearer_token: str | None = None,
    issuer: str | None = None,
    audience: Iterable[str] = (),
    authorized_parties: Iterable[str] = (),
    clock_skew_seconds: float = 5.0,
    now: float | None = None,
) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise ClerkAuthError("Clerk session token is malformed")

    header = _json_b64url(parts[0], "JWT header")
    payload = _json_b64url(parts[1], "JWT payload")
    if header.get("alg") != "RS256":
        raise ClerkAuthError("Clerk session token must use RS256")

    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    signature = _b64url_decode(parts[2])
    jwk = _select_jwk(
        _load_jwks(jwks_json=jwks_json, jwks_url=jwks_url, jwks_bearer_token=jwks_bearer_token),
        header.get("kid"),
    )
    if not _verify_rs256(signing_input, signature, jwk):
        raise ClerkAuthError("Clerk session token signature is invalid")

    _validate_claims(
        payload,
        issuer=issuer,
        audience=list(audience),
        authorized_parties=list(authorized_parties),
        clock_skew_seconds=clock_skew_seconds,
        now=time.time() if now is None else now,
    )
    return payload


def _json_b64url(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(_b64url_decode(value).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ClerkAuthError(f"Clerk {label} is invalid") from exc
    if not isinstance(parsed, dict):
        raise ClerkAuthError(f"Clerk {label} must be a JSON object")
    return parsed


def _b64url_decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ClerkAuthError("Clerk token contains invalid base64url") from exc


def _load_jwks(*, jwks_json: str | None, jwks_url: str | None, jwks_bearer_token: str | None) -> dict[str, Any]:
    if jwks_json:
        try:
            parsed = json.loads(jwks_json)
        except json.JSONDecodeError as exc:
            raise ClerkAuthError("CLERK_JWKS_JSON is invalid JSON") from exc
        return _normalise_jwks(parsed)
    if not jwks_url:
        raise ClerkAuthError("CLERK_JWKS_URL or CLERK_JWKS_JSON must be configured")

    now = time.time()
    cache_key = f"{jwks_url}\0{jwks_bearer_token or ''}"
    with _jwks_lock:
        cached = _jwks_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

    request = urllib.request.Request(jwks_url, headers={"Accept": "application/json"})
    if jwks_bearer_token:
        request.add_header("Authorization", f"Bearer {jwks_bearer_token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise ClerkAuthError("Could not fetch Clerk JWKS") from exc
    jwks = _normalise_jwks(parsed)
    with _jwks_lock:
        _jwks_cache[cache_key] = (now + _JWKS_CACHE_SECONDS, jwks)
    return jwks


def _normalise_jwks(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get("keys"), list):
        return value
    if isinstance(value, dict) and value.get("kty"):
        return {"keys": [value]}
    raise ClerkAuthError("Clerk JWKS must contain a keys array")


def _select_jwk(jwks: dict[str, Any], kid: Any) -> dict[str, Any]:
    keys = [key for key in jwks.get("keys", []) if isinstance(key, dict)]
    if kid:
        for key in keys:
            if key.get("kid") == kid:
                return key
        raise ClerkAuthError("Clerk JWKS does not contain the token key")
    if len(keys) == 1:
        return keys[0]
    raise ClerkAuthError("Clerk session token is missing a key id")


def _verify_rs256(signing_input: bytes, signature: bytes, jwk: dict[str, Any]) -> bool:
    if jwk.get("kty") != "RSA":
        raise ClerkAuthError("Clerk JWKS key must be RSA")
    try:
        modulus = int.from_bytes(_b64url_decode(str(jwk["n"])), "big")
        exponent = int.from_bytes(_b64url_decode(str(jwk["e"])), "big")
    except KeyError as exc:
        raise ClerkAuthError("Clerk RSA JWK is missing n or e") from exc
    key_bytes = (modulus.bit_length() + 7) // 8
    if not signature or len(signature) != key_bytes:
        return False

    encoded = pow(int.from_bytes(signature, "big"), exponent, modulus).to_bytes(key_bytes, "big")
    digest = hashlib.sha256(signing_input).digest()
    expected_tail = _SHA256_DIGESTINFO_PREFIX + digest
    if not encoded.startswith(b"\x00\x01") or not encoded.endswith(expected_tail):
        return False
    separator = encoded.find(b"\x00", 2)
    if separator < 10:
        return False
    padding = encoded[2:separator]
    return all(byte == 0xFF for byte in padding) and hmac.compare_digest(encoded[separator + 1 :], expected_tail)


def _validate_claims(
    payload: dict[str, Any],
    *,
    issuer: str | None,
    audience: list[str],
    authorized_parties: list[str],
    clock_skew_seconds: float,
    now: float,
) -> None:
    subject = str(payload.get("sub") or "").strip()
    if not subject:
        raise ClerkAuthError("Clerk session token is missing subject")

    exp = _numeric_claim(payload, "exp")
    if exp is None or exp < now - clock_skew_seconds:
        raise ClerkAuthError("Clerk session token is expired")
    nbf = _numeric_claim(payload, "nbf")
    if nbf is not None and nbf > now + clock_skew_seconds:
        raise ClerkAuthError("Clerk session token is not active yet")
    iat = _numeric_claim(payload, "iat")
    if iat is not None and iat > now + clock_skew_seconds:
        raise ClerkAuthError("Clerk session token was issued in the future")

    if issuer and payload.get("iss") != issuer:
        raise ClerkAuthError("Clerk session token issuer mismatch")

    if audience and not _claim_matches_any(payload.get("aud"), audience):
        raise ClerkAuthError("Clerk session token audience mismatch")

    if authorized_parties and payload.get("azp") not in authorized_parties:
        raise ClerkAuthError("Clerk session token authorized party mismatch")


def _numeric_claim(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ClerkAuthError(f"Clerk session token {key} claim is invalid") from exc


def _claim_matches_any(value: Any, expected: list[str]) -> bool:
    if isinstance(value, str):
        values = {value}
    elif isinstance(value, list):
        values = {str(item) for item in value}
    else:
        return False
    return bool(values.intersection(expected))
