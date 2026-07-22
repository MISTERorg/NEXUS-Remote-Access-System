"""
core/auth.py
------------
Authentication & authorization subsystem.

Responsibilities:
- User credential verification (password + optional TOTP MFA)
- JWT access/refresh token issuance and validation
- Device certificate verification (mTLS device identity)
- Role-Based Access Control (RBAC) enforcement
- Login-attempt tracking and lockout
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

try:
    import jwt
    if not hasattr(jwt, "encode"):
        raise ImportError(
            "The installed 'jwt' package is missing '.encode()'. "
            "You likely installed 'jwt' instead of 'PyJWT'. "
            "Fix by running: pip uninstall jwt PyJWT -y && pip install PyJWT"
        )
except ImportError as _jwt_err:
    raise RuntimeError(str(_jwt_err)) from _jwt_err

from pydantic import BaseModel, Field

from config.settings import settings
from utils.crypto import hash_password, verify_password
from utils.logger import AuditLogger, get_logger

log = get_logger("nexus.auth")
audit = AuditLogger(settings.log.audit_file)


# ---------------------------------------------------------------------------
# RBAC roles & permissions
# ---------------------------------------------------------------------------

class Role(str, Enum):
    ADMIN = "admin"           # Full access
    OPERATOR = "operator"     # Connect + control + file transfer
    VIEWER = "viewer"         # Screen view only, no input
    AGENT = "agent"           # Internal: device agent identity


ROLE_PERMISSIONS: Dict[Role, List[str]] = {
    Role.ADMIN:    ["*"],
    Role.OPERATOR: ["session.open", "session.close", "screen.view", "screen.control",
                    "terminal.open", "file.upload", "file.download", "clipboard"],
    Role.VIEWER:   ["session.open", "screen.view"],
    Role.AGENT:    ["agent.register", "agent.heartbeat", "agent.stream"],
}


def has_permission(role: Role, permission: str) -> bool:
    perms = ROLE_PERMISSIONS.get(role, [])
    return "*" in perms or permission in perms


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class UserRecord(BaseModel):
    """Stored user record."""
    user_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    username: str
    password_hash: str
    role: Role = Role.OPERATOR
    mfa_secret: Optional[str] = None       # TOTP secret (base32)
    is_active: bool = True
    failed_attempts: int = 0
    locked_until: Optional[float] = None   # Unix timestamp

    def is_locked(self) -> bool:
        if self.locked_until is None:
            return False
        return time.time() < self.locked_until


class TokenPayload(BaseModel):
    sub: str           # subject (user_id)
    role: str
    exp: float
    jti: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str = "access"


class DeviceIdentity(BaseModel):
    device_id: str
    common_name: str   # from TLS cert CN
    role: Role = Role.AGENT
    is_trusted: bool = True


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

class TokenManager:
    """Issues and validates JWT tokens."""

    _revoked: set[str] = set()   # jti revocation list

    @classmethod
    def issue_access_token(cls, user_id: str, role: Role) -> str:
        payload = {
            "sub": user_id,
            "role": role.value,
            "exp": datetime.now(timezone.utc) + timedelta(
                minutes=settings.auth.access_token_expire_minutes
            ),
            "jti": str(uuid.uuid4()),
            "type": "access",
        }
        return jwt.encode(payload, settings.auth.secret_key, algorithm=settings.auth.algorithm)

    @classmethod
    def issue_refresh_token(cls, user_id: str, role: Role) -> str:
        payload = {
            "sub": user_id,
            "role": role.value,
            "exp": datetime.now(timezone.utc) + timedelta(
                days=settings.auth.refresh_token_expire_days
            ),
            "jti": str(uuid.uuid4()),
            "type": "refresh",
        }
        return jwt.encode(payload, settings.auth.secret_key, algorithm=settings.auth.algorithm)

    @classmethod
    def verify_token(cls, token: str, expected_type: str = "access") -> TokenPayload:
        try:
            data = jwt.decode(
                token,
                settings.auth.secret_key,
                algorithms=[settings.auth.algorithm],
            )
        except jwt.ExpiredSignatureError:
            raise AuthError("Token expired")
        except jwt.InvalidTokenError as e:
            raise AuthError(f"Invalid token: {e}")

        if data.get("type") != expected_type:
            raise AuthError(f"Wrong token type: expected {expected_type}")
        if data.get("jti") in cls._revoked:
            raise AuthError("Token has been revoked")

        return TokenPayload(**data)

    @classmethod
    def revoke(cls, jti: str) -> None:
        cls._revoked.add(jti)


# ---------------------------------------------------------------------------
# In-memory user store
# ---------------------------------------------------------------------------

class UserStore:
    def __init__(self):
        self._users: Dict[str, UserRecord] = {}
        self._seed_admin()

    def _seed_admin(self) -> None:
        admin = UserRecord(
            username="admin",
            password_hash=hash_password("admin123"),
            role=Role.ADMIN,
        )
        self._users[admin.username] = admin

    def get(self, username: str) -> Optional[UserRecord]:
        return self._users.get(username)

    def add(self, user: UserRecord) -> None:
        self._users[user.username] = user

    def save(self, user: UserRecord) -> None:
        self._users[user.username] = user


# ---------------------------------------------------------------------------
# Auth service
# ---------------------------------------------------------------------------

class AuthError(Exception):
    """Raised on authentication/authorization failure."""


class AuthService:
    def __init__(self, store: Optional[UserStore] = None):
        self._store = store or UserStore()

    def authenticate(self, username: str, password: str, totp_code: Optional[str] = None) -> Dict[str, Any]:
        user = self._store.get(username)
        if not user:
            audit.login_failure(username, ip="unknown", reason="user_not_found")
            raise AuthError("Invalid credentials")

        if user.is_locked():
            audit.login_failure(user.user_id, ip="unknown", reason="account_locked")
            raise AuthError("Account temporarily locked")

        if not user.is_active:
            raise AuthError("Account disabled")

        if not verify_password(password, user.password_hash):
            user.failed_attempts += 1
            if user.failed_attempts >= settings.auth.max_login_attempts:
                user.locked_until = time.time() + settings.auth.lockout_minutes * 60
                log.warning("auth.account_locked", username=username)
            self._store.save(user)
            audit.login_failure(user.user_id, ip="unknown", reason="bad_password")
            raise AuthError("Invalid credentials")

        mfa_used = False
        if settings.auth.mfa_enabled and user.mfa_secret:
            if not totp_code:
                raise AuthError("MFA code required")
            if not self._verify_totp(user.mfa_secret, totp_code):
                audit.login_failure(user.user_id, ip="unknown", reason="bad_mfa")
                raise AuthError("Invalid MFA code")
            mfa_used = True

        user.failed_attempts = 0
        user.locked_until = None
        self._store.save(user)

        audit.login_success(user.user_id, ip="unknown", mfa=mfa_used)
        log.info("auth.login_success", username=username, role=user.role)

        return {
            "access_token": TokenManager.issue_access_token(user.user_id, user.role),
            "refresh_token": TokenManager.issue_refresh_token(user.user_id, user.role),
            "token_type": "Bearer",
            "role": user.role.value,
        }

    def verify_access_token(self, token: str) -> TokenPayload:
        return TokenManager.verify_token(token, expected_type="access")

    def refresh(self, refresh_token: str) -> Dict[str, str]:
        payload = TokenManager.verify_token(refresh_token, expected_type="refresh")
        role = Role(payload.role)
        return {
            "access_token": TokenManager.issue_access_token(payload.sub, role),
            "token_type": "Bearer",
        }

    def logout(self, token: str) -> None:
        try:
            payload = TokenManager.verify_token(token)
            TokenManager.revoke(payload.jti)
        except AuthError:
            pass

    def require_permission(self, token: str, permission: str) -> TokenPayload:
        payload = self.verify_access_token(token)
        role = Role(payload.role)
        if not has_permission(role, permission):
            audit.permission_denied(payload.sub, resource="api", action=permission)
            raise AuthError(f"Permission denied: {permission}")
        return payload

    def register_user(self, username: str, password: str, role: Role = Role.OPERATOR) -> UserRecord:
        if self._store.get(username):
            raise AuthError("Username already exists")
        user = UserRecord(
            username=username,
            password_hash=hash_password(password),
            role=role,
        )
        self._store.add(user)
        log.info("auth.user_registered", username=username, role=role)
        return user

    @staticmethod
    def _verify_totp(secret: str, code: str) -> bool:
        try:
            import pyotp
            totp = pyotp.TOTP(secret)
            return totp.verify(code, valid_window=1)
        except ImportError:
            log.warning("auth.pyotp_missing", msg="MFA check skipped - install pyotp")
            return True


auth_service = AuthService()