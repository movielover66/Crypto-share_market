"""
TRADEIQ Crypto v1.0 — Settings & Single-Owner HMAC-SHA256 JWT Authentication.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException, status


@dataclass(frozen=True)
class CryptoSettings:
    demo_mode: bool = os.getenv("DEMO_MODE", "false").lower() == "true"
    app_env: str = os.getenv("APP_ENV", "production")
    owner_id: str = "owner-crypto-1"
    owner_password: str = os.getenv("OWNER_PASSWORD", "")
    owner_password_hash: str = os.getenv("OWNER_PASSWORD_HASH", "")
    jwt_secret: str = os.getenv("AUTH_JWT_SECRET", "dev-insecure-change-me-in-render")
    jwt_ttl_sec: int = int(os.getenv("AUTH_JWT_TTL_SEC", "86400"))
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_key: str = os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    gemini_api_keys: str = os.getenv("GEMINI_API_KEYS", "") or os.getenv("GEMINI_API_KEY", "")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")


def get_settings() -> CryptoSettings:
    return CryptoSettings()


def verify_owner_password(plain: str, cfg: CryptoSettings) -> bool:
    if cfg.owner_password_hash:
        digest = hashlib.sha256(plain.encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, cfg.owner_password_hash.strip()) or hmac.compare_digest(plain, cfg.owner_password_hash.strip())
    if cfg.owner_password:
        return hmac.compare_digest(plain, cfg.owner_password)
    if cfg.demo_mode or cfg.app_env == "test":
        return hmac.compare_digest(plain, "tradeiq-test-pass")
    return False


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(txt: str) -> bytes:
    pad = "=" * ((4 - len(txt) % 4) % 4)
    return base64.urlsafe_b64decode(txt + pad)


def issue_owner_token(cfg: CryptoSettings) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    now = int(time.time())
    payload = _b64url(
        json.dumps({"sub": cfg.owner_id, "iat": now, "exp": now + cfg.jwt_ttl_sec}, separators=(",", ":")).encode()
    )
    sig = _b64url(hmac.new(cfg.jwt_secret.encode("utf-8"), f"{header}.{payload}".encode("ascii"), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def verify_owner_token(token: str, cfg: CryptoSettings) -> Optional[str]:
    if not token or token.count(".") != 2:
        return None
    try:
        h, p, s = token.split(".")
        expected = _b64url(hmac.new(cfg.jwt_secret.encode("utf-8"), f"{h}.{p}".encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(s, expected):
            return None
        claims = json.loads(_b64url_decode(p).decode("utf-8"))
        if int(claims.get("exp", 0)) <= int(time.time()):
            return None
        return str(claims.get("sub") or cfg.owner_id)
    except Exception:
        return None


def require_owner(authorization: Optional[str] = Header(None)) -> str:
    cfg = get_settings()
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing owner bearer token")
    token = authorization.split(" ", 1)[1].strip()
    sub = verify_owner_token(token, cfg)
    if not sub:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired owner token")
    return sub
