"""Auth services on the SQLite system of record: registration, authentication, server-side sessions,
CSRF, rate limiting / lockout, plan assignment and entitlement resolution.

Security notes (details and constants in shots.auth.settings and docs/platform.md, "Authentication"):
- Session ids are 256-bit random tokens; the DB stores only SHA-256(token). The CSRF token of a
  session is HMAC-SHA256(key=session token, "shots-csrf-v1"): derivable only by whoever holds the
  HttpOnly cookie, so it needs no storage and dies with the session (rotated on login).
- Login / register forms of visitors without a session use a stateless pre-login CSRF token
  ("<issued epoch s>.<HMAC-SHA256(server key, nonce|issued)>") bound to a random nonce in an HttpOnly
  cookie: no DB row per anonymous page view, valid PRELOGIN_CSRF_TTL.
- Emails and client IPs in login_attempts / sessions are HMAC-SHA256 with a server key (not plain
  hashes, which a dictionary would reverse).
- Errors are codes from a small fixed set; login never says whether an email exists.
- Nothing here logs a password, a token, a session id or an email.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from sqlalchemy import Engine, delete, func, or_, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shots.auth import passwords as pw
from shots.auth import settings as S
from shots.db.access import Entitlements, default_plan_key, for_plan, plan_values
from shots.db.engine import make_engine, session_scope
from shots.db.models import LoginAttempt, Plan, Subscription, User, UserSession, utcnow

log = logging.getLogger(__name__)

ACTIVE_SUB_STATUSES = ("active", "trialing")
_CSRF_INFO = b"shots-csrf-v1"
_PRELOGIN_INFO = "shots-prelogin-csrf-v1"
_EPOCH = datetime(1970, 1, 1)
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class AuthError(Exception):
    """code: invalid_credentials | rate_limited | invalid_email | weak_password | password_mismatch |
    email_taken | registration_failed | not_found | invalid_plan. reason: sub-code (weak_password)."""

    def __init__(self, code: str, reason: str | None = None):
        super().__init__(code if reason is None else f"{code}:{reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class UserInfo:
    """Detached snapshot of a user (safe to hand to templates: no password hash)."""
    id: int
    email: str
    display_name: str | None
    is_admin: bool
    is_active: bool
    created_at: datetime

    @classmethod
    def of(cls, u: User) -> "UserInfo":
        return cls(u.id, u.email, u.display_name, bool(u.is_admin), bool(u.is_active), u.created_at)


@dataclass(frozen=True)
class NewSession:
    """A freshly created session. `token` goes into the cookie only; never log or store it."""
    token: str
    expires_at: datetime
    max_age: int  # seconds, for the cookie
    csrf: str

    def __repr__(self) -> str:  # keep tokens out of tracebacks and logs
        return f"NewSession(expires_at={self.expires_at!r})"


@dataclass(frozen=True)
class SessionState:
    id_hash: str
    user: UserInfo | None
    csrf: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime

    def __repr__(self) -> str:
        return f"SessionState(user_id={self.user.id if self.user else None}, expires_at={self.expires_at!r})"


@dataclass(frozen=True)
class PlanInfo:
    key: str  # display / admin only: authorization uses entitlements, never this key
    name: str
    sort_order: int
    is_default: bool
    entitlements: dict


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_for(token: str) -> str:
    mac = hmac.new(token.encode(), _CSRF_INFO, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def _load_secret(engine: Engine) -> bytes:
    """SHOTS_AUTH_KEY, else <db dir>/auth.key (created on first use, git-ignored), else (in-memory
    DB) an ephemeral random key. Losing the key only resets rate-limit history."""
    env = os.environ.get("SHOTS_AUTH_KEY")
    if env:
        return env.encode()
    db = make_url(str(engine.url)).database
    if not db or db == ":memory:":
        return secrets.token_bytes(32)
    path = Path(db).with_name("auth.key")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return bytes.fromhex(path.read_text().strip())
    with os.fdopen(fd, "w") as fh:
        fh.write(secrets.token_hex(32))
    return bytes.fromhex(path.read_text().strip())


class AuthService:
    def __init__(self, db: str | Path | Engine | None = None, secret: bytes | None = None,
                 clock: Callable[[], datetime] | None = None, anonymous_plan: str | None = None):
        """db: URL / path / Engine (default SHOTS_DB). clock: naive-UTC now (tests).
        anonymous_plan: plan key whose entitlements anonymous visitors get (config, not code;
        default = the is_default plan, i.e. Free). An unknown key denies everything."""
        self.engine = make_engine(db)
        self._secret = secret
        self.now = clock or utcnow
        self.anonymous_plan = anonymous_plan if anonymous_plan is not None else os.environ.get("SHOTS_ANON_PLAN")

    # ------------------------------------------------------------------ keys
    def _key(self) -> bytes:
        if self._secret is None:
            self._secret = _load_secret(self.engine)
        return self._secret

    def keyed_hash(self, kind: str, value: str | None) -> str | None:
        if not value:
            return None
        return hmac.new(self._key(), f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()

    def account_key(self, email: str) -> str | None:
        return self.keyed_hash("account", pw.normalize_email(email))

    # ------------------------------------------------------------------ rate limits
    def _consecutive_failures(self, s: Session, kind: str, account: str | None, now: datetime):
        """(failures since the last success within LOCKOUT_WINDOW, time of the last failure)."""
        if account is None:
            return 0, None
        rows = s.execute(select(LoginAttempt.success, LoginAttempt.at)
                         .where(LoginAttempt.kind == kind, LoginAttempt.account_hash == account,
                                LoginAttempt.at >= now - S.LOCKOUT_WINDOW)
                         .order_by(LoginAttempt.at.desc(), LoginAttempt.id.desc())).all()
        n, last = 0, None
        for success, at in rows:
            if success:
                break
            n += 1
            last = last or at
        return n, last

    def lockout_until(self, s: Session, kind: str, account: str | None, now: datetime) -> datetime | None:
        n, last = self._consecutive_failures(s, kind, account, now)
        if n < S.LOGIN_FREE_FAILURES or last is None:
            return None
        delay = min(S.LOCKOUT_BASE * (2 ** (n - S.LOGIN_FREE_FAILURES)), S.LOCKOUT_MAX)
        until = last + delay
        return until if now < until else None

    def _count(self, s: Session, kind: str, now: datetime, window: timedelta, *, client: str | None = None,
               account: str | None = None, failures_only: bool = False) -> int:
        q = select(func.count()).select_from(LoginAttempt).where(LoginAttempt.kind == kind,
                                                                 LoginAttempt.at >= now - window)
        q = q.where(LoginAttempt.client_hash == client) if client is not None else q
        q = q.where(LoginAttempt.account_hash == account) if account is not None else q
        q = q.where(LoginAttempt.success.is_(False)) if failures_only else q
        return int(s.scalar(q) or 0)

    def login_blocked(self, s: Session, account: str | None, client: str | None, now: datetime,
                      kind: str = "login") -> bool:
        if self.lockout_until(s, kind, account, now) is not None:
            return True
        return client is not None and self._count(s, kind, now, S.LOGIN_CLIENT_WINDOW, client=client,
                                                  failures_only=True) >= S.LOGIN_CLIENT_MAX_FAILURES

    def register_blocked(self, s: Session, account: str | None, client: str | None, now: datetime) -> bool:
        if client is not None and self._count(s, "register", now, S.REGISTER_CLIENT_WINDOW,
                                              client=client) >= S.REGISTER_CLIENT_MAX:
            return True
        return account is not None and self._count(s, "register", now, S.REGISTER_ACCOUNT_WINDOW,
                                                   account=account) >= S.REGISTER_ACCOUNT_MAX

    @staticmethod
    def _record(s: Session, kind: str, account: str | None, client: str | None, success: bool, now: datetime):
        s.add(LoginAttempt(kind=kind, account_hash=account, client_hash=client, success=success, at=now))

    @staticmethod
    def _write_lock(s: Session) -> None:
        """Take SQLite's database write lock now (a no-op UPDATE starts the write transaction), so the
        check-and-record of an attempt is atomic across threads and processes: concurrent attempts
        queue on the lock (busy_timeout) and each one sees the attempts recorded before it."""
        s.execute(update(LoginAttempt).where(LoginAttempt.id == -1).values(success=False))

    def _reserve(self, kind: str, account: str | None, client: str | None, now: datetime,
                 blocked: Callable[[Session], bool], load: Callable[[Session], object] | None = None):
        """Atomically: refuse (AuthError rate_limited, nothing recorded) if `blocked`, else record the
        attempt as a failure *before* any password work, so N parallel guesses cannot all pass the check
        before any of them is counted. Returns (attempt id, load(s)). The caller marks the attempt
        successful afterwards with _mark_success."""
        with session_scope(self.engine) as s:
            self._write_lock(s)
            if blocked(s):
                raise AuthError("rate_limited")
            row = LoginAttempt(kind=kind, account_hash=account, client_hash=client, success=False, at=now)
            s.add(row)
            s.flush()
            return row.id, (load(s) if load else None)

    def _mark_success(self, s: Session, attempt_id: int) -> None:
        s.execute(update(LoginAttempt).where(LoginAttempt.id == attempt_id).values(success=True))

    # ------------------------------------------------------------------ registration / users
    def register(self, email: str, password: str, client: str | None = None,
                 password_confirm: str | None = None) -> UserInfo:
        """Open registration: the new user gets the default plan. Raises AuthError."""
        now = self.now()
        norm = pw.normalize_email(email)
        account, client_h = self.account_key(norm), self.keyed_hash("client", client)
        # every attempt that gets past the limiter counts, success or not: recorded atomically with the check
        attempt, _ = self._reserve("register", account, client_h, now,
                                   lambda s: self.register_blocked(s, account, client_h, now))
        try:
            user = self._create_user(norm, password, password_confirm=password_confirm)
        except AuthError as exc:
            if exc.code == "email_taken" and not S.REVEAL_EMAIL_TAKEN:
                raise AuthError("registration_failed") from None
            raise
        with session_scope(self.engine) as s:
            self._mark_success(s, attempt)
        log.info("user registered: id=%s", user.id)
        return user

    def _create_user(self, email: str, password: str, *, password_confirm: str | None = None,
                     plan: str | None = None, is_admin: bool = False) -> UserInfo:
        norm = pw.normalize_email(email)
        if not pw.valid_email(norm):
            raise AuthError("invalid_email")
        if password_confirm is not None and password_confirm != password:
            raise AuthError("password_mismatch")
        problem = pw.password_problem(password, norm)
        if problem:
            raise AuthError("weak_password", problem)
        encoded = pw.hash_password(password)  # hashed before the existence check: same cost either way
        now = self.now()
        try:
            with session_scope(self.engine) as s:
                if s.scalar(select(User.id).where(User.email == norm)) is not None:
                    raise AuthError("email_taken")
                plan_key = plan or default_plan_key(s)
                if plan is not None and self._plan(s, plan) is None:
                    raise AuthError("invalid_plan")
                user = User(email=norm, password_hash=encoded, is_active=True, is_admin=is_admin,
                            password_changed_at=now, created_at=now, updated_at=now)
                s.add(user)
                s.flush()
                if plan_key:
                    self._assign(s, user.id, plan_key, now)
                return UserInfo.of(user)
        except IntegrityError:  # concurrent registration of the same email
            raise AuthError("email_taken") from None

    def create_user(self, email: str, password: str, plan: str | None = None, is_admin: bool = False) -> UserInfo:
        """Admin path (CLI): same validation as registration, no rate limit, explicit plan."""
        return self._create_user(email, password, plan=plan, is_admin=is_admin)

    def get_user(self, user_id: int) -> UserInfo | None:
        with session_scope(self.engine) as s:
            u = s.get(User, user_id)
            return UserInfo.of(u) if u else None

    def find_user(self, email: str) -> UserInfo | None:
        with session_scope(self.engine) as s:
            u = s.scalar(select(User).where(User.email == pw.normalize_email(email)))
            return UserInfo.of(u) if u else None

    def list_users(self) -> list[tuple[UserInfo, str | None]]:
        """(user, current plan key) for every user, oldest first (admin CLI)."""
        with session_scope(self.engine) as s:
            users = s.scalars(select(User).order_by(User.id)).all()
            return [(UserInfo.of(u), (self._active_plan(s, u.id) or (None,))[0]) for u in users]

    def set_active(self, user_id: int, active: bool) -> None:
        now = self.now()
        with session_scope(self.engine) as s:
            u = s.get(User, user_id)
            if u is None:
                raise AuthError("not_found")
            u.is_active, u.updated_at = active, now
            if not active:
                self._revoke_user(s, user_id, now)

    def set_admin(self, user_id: int, admin: bool) -> None:
        with session_scope(self.engine) as s:
            u = s.get(User, user_id)
            if u is None:
                raise AuthError("not_found")
            u.is_admin, u.updated_at = admin, self.now()

    # ------------------------------------------------------------------ authentication
    def authenticate(self, email: str, password: str, client: str | None = None) -> UserInfo:
        """Raises AuthError('rate_limited') or AuthError('invalid_credentials') (unknown email, wrong
        password and deactivated account are indistinguishable, and cost one Argon2 verify each)."""
        now = self.now()
        norm = pw.normalize_email(email)
        account, client_h = self.account_key(norm), self.keyed_hash("client", client)

        def load(s: Session):
            u = s.scalar(select(User).where(User.email == norm)) if norm else None
            return (u.id, u.password_hash, bool(u.is_active)) if u else None

        # check + record (as a failure) in one write transaction, then verify outside of it
        attempt, found = self._reserve("login", account, client_h, now,
                                       lambda s: self.login_blocked(s, account, client_h, now), load)
        ok = pw.verify_password(found[1] if found else None, password or "")
        ok = ok and found is not None and found[2]
        if not ok:
            log.info("login failed")
            raise AuthError("invalid_credentials")
        rehash = pw.hash_password(password) if pw.needs_rehash(found[1]) else None
        with session_scope(self.engine) as s:
            self._mark_success(s, attempt)
            user = s.get(User, found[0])
            if user is None or not user.is_active:  # deleted / deactivated meanwhile
                raise AuthError("invalid_credentials")
            if rehash and user.password_hash == found[1]:
                user.password_hash = rehash
            log.info("login ok: user_id=%s", user.id)
            return UserInfo.of(user)

    def login(self, email: str, password: str, client: str | None = None, user_agent: str | None = None,
              old_token: str | None = None) -> tuple[UserInfo, NewSession]:
        """authenticate + session rotation: the previous (anonymous or user) session is revoked and a
        new id issued, so a planted pre-login id never becomes an authenticated one."""
        user = self.authenticate(email, password, client)
        return user, self.create_session(user.id, client, user_agent, replace_token=old_token)

    def change_password(self, user_id: int, current: str, new: str, new_confirm: str | None = None,
                        client: str | None = None, user_agent: str | None = None) -> NewSession:
        """Checks the current password (login backoff, kind 'password'), applies the policy, revokes
        every session of the user (this one included) and returns a fresh session for this browser."""
        now = self.now()
        with session_scope(self.engine) as s:
            user = s.get(User, user_id)
            if user is None or not user.is_active:
                raise AuthError("invalid_credentials")
            email, stored = user.email, user.password_hash
        account, client_h = self.account_key(email), self.keyed_hash("client", client)
        attempt, _ = self._reserve("password", account, client_h, now,
                                   lambda s: self.login_blocked(s, account, None, now, kind="password"))
        if not pw.verify_password(stored, current or ""):
            raise AuthError("invalid_credentials")
        with session_scope(self.engine) as s:
            self._mark_success(s, attempt)  # kept even if a check below fails
        with session_scope(self.engine) as s:
            user = s.get(User, user_id)
            if user is None or not user.is_active or user.password_hash != stored:
                raise AuthError("invalid_credentials")  # deactivated or changed concurrently
            if new_confirm is not None and new != new_confirm:
                raise AuthError("password_mismatch")
            problem = pw.password_problem(new, user.email)
            if problem is None and pw.normalize_password(new) == pw.normalize_password(current):
                problem = "same"
            if problem:
                raise AuthError("weak_password", problem)
            user.password_hash = pw.hash_password(new)
            user.password_changed_at = user.updated_at = now
            self._revoke_user(s, user_id, now)
        log.info("password changed: user_id=%s", user_id)
        return self.create_session(user_id, client, user_agent)

    # ------------------------------------------------------------------ sessions
    def create_session(self, user_id: int | None, client: str | None = None, user_agent: str | None = None,
                       replace_token: str | None = None) -> NewSession:
        """user_id None = anonymous pre-login session (CSRF for login/register forms)."""
        now = self.now()
        token = secrets.token_urlsafe(S.SESSION_TOKEN_BYTES)
        life = S.SESSION_ABSOLUTE_TIMEOUT if user_id is not None else S.ANON_SESSION_TIMEOUT
        csrf = csrf_for(token)
        with session_scope(self.engine) as s:
            if replace_token:
                s.execute(update(UserSession).where(UserSession.id_hash == sha256_hex(replace_token),
                                                    UserSession.revoked_at.is_(None)).values(revoked_at=now))
            s.add(UserSession(id_hash=sha256_hex(token), user_id=user_id, csrf_hash=sha256_hex(csrf),
                              created_at=now, last_seen_at=now, expires_at=now + life,
                              ip_hash=self.keyed_hash("client", client),
                              ua_hash=self.keyed_hash("ua", user_agent)))
        return NewSession(token=token, expires_at=now + life, max_age=int(life.total_seconds()), csrf=csrf)

    def validate(self, token: str | None, touch: bool = True) -> SessionState | None:
        """The live session behind a cookie value, or None (unknown, revoked, idle or absolute expiry,
        deactivated user). Refreshes last_seen_at at most every SESSION_TOUCH_INTERVAL."""
        if not token or len(token) > 128:
            return None
        now = self.now()
        with session_scope(self.engine) as s:
            row = s.get(UserSession, sha256_hex(token))
            if row is None or row.revoked_at is not None or now >= row.expires_at:
                return None
            idle = S.SESSION_IDLE_TIMEOUT if row.user_id is not None else S.ANON_SESSION_TIMEOUT
            if now - row.last_seen_at >= idle:
                return None
            user = None
            if row.user_id is not None:
                u = s.get(User, row.user_id)
                if u is None or not u.is_active:
                    return None
                user = UserInfo.of(u)
            if touch and now - row.last_seen_at >= S.SESSION_TOUCH_INTERVAL:
                row.last_seen_at = now
            return SessionState(row.id_hash, user, csrf_for(token), row.created_at, row.last_seen_at,
                                row.expires_at)

    # ------------------------------------------------------------------ pre-login CSRF (stateless)
    @staticmethod
    def new_prelogin_nonce() -> str:
        return secrets.token_urlsafe(S.PRELOGIN_NONCE_BYTES)

    @staticmethod
    def valid_prelogin_nonce(nonce: str | None) -> bool:
        return bool(nonce) and isinstance(nonce, str) and _NONCE_RE.match(nonce) is not None

    def _prelogin_mac(self, nonce: str, issued: int) -> str:
        mac = hmac.new(self._key(), f"{_PRELOGIN_INFO}|{nonce}|{issued}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()

    def prelogin_csrf(self, nonce: str) -> str:
        """CSRF token of the login / register forms for a visitor without a session: bound to the
        random `nonce` of their pre-login cookie and to the issue time; needs no storage."""
        if not self.valid_prelogin_nonce(nonce):
            raise ValueError("invalid pre-login nonce")
        issued = int((self.now() - _EPOCH).total_seconds())
        return f"{issued}.{self._prelogin_mac(nonce, issued)}"

    def check_prelogin_csrf(self, nonce: str | None, submitted: str | None) -> bool:
        """True iff `submitted` was issued by prelogin_csrf(nonce) less than PRELOGIN_CSRF_TTL ago
        (constant-time MAC comparison)."""
        if not self.valid_prelogin_nonce(nonce) or not submitted or len(submitted) > 128:
            return False
        stamp, _, mac = submitted.partition(".")
        if not stamp.isdigit() or not mac or len(stamp) > 12:
            return False
        issued = int(stamp)
        age = (self.now() - _EPOCH).total_seconds() - issued
        if age < -S.PRELOGIN_CSRF_SKEW.total_seconds() or age >= S.PRELOGIN_CSRF_TTL.total_seconds():
            return False
        return hmac.compare_digest(self._prelogin_mac(nonce, issued).encode(), mac.encode())

    def check_csrf(self, token: str | None, submitted: str | None) -> bool:
        """True iff `token` is a live session and `submitted` is its CSRF token (constant-time)."""
        if not submitted or not token or self.validate(token, touch=False) is None:
            return False
        return hmac.compare_digest(csrf_for(token).encode(), submitted.encode())

    def revoke(self, token: str | None) -> None:
        """Logout: revoke this session (unknown tokens are ignored)."""
        if not token:
            return
        with session_scope(self.engine) as s:
            s.execute(update(UserSession).where(UserSession.id_hash == sha256_hex(token),
                                                UserSession.revoked_at.is_(None)).values(revoked_at=self.now()))

    logout = revoke

    @staticmethod
    def _revoke_user(s: Session, user_id: int, now: datetime, except_hash: str | None = None) -> int:
        q = update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        if except_hash:
            q = q.where(UserSession.id_hash != except_hash)
        return s.execute(q.values(revoked_at=now)).rowcount

    def revoke_all(self, user_id: int, except_token: str | None = None) -> int:
        """Revoke every live session of a user (optionally keeping one); returns how many."""
        with session_scope(self.engine) as s:
            return self._revoke_user(s, user_id, self.now(), sha256_hex(except_token) if except_token else None)

    def purge(self) -> dict:
        """Delete long-dead sessions and old attempts (housekeeping; safe to run any time)."""
        now = self.now()
        cut = now - S.SESSION_PURGE_AFTER
        with session_scope(self.engine) as s:
            dead = or_(UserSession.expires_at < cut, UserSession.revoked_at < cut,
                       UserSession.last_seen_at < cut - S.SESSION_IDLE_TIMEOUT)
            n_s = s.execute(delete(UserSession).where(dead)).rowcount
            n_a = s.execute(delete(LoginAttempt).where(LoginAttempt.at < now - S.ATTEMPTS_PURGE_AFTER)).rowcount
        return {"sessions": n_s, "attempts": n_a}

    # ------------------------------------------------------------------ plans and entitlements
    @staticmethod
    def _plan(s: Session, key: str) -> Plan | None:
        return s.scalar(select(Plan).where(Plan.key == key, Plan.is_active.is_(True)))

    def _assign(self, s: Session, user_id: int, plan_key: str, now: datetime, provider: str = "manual") -> None:
        plan = self._plan(s, plan_key)
        if plan is None:
            raise AuthError("invalid_plan")
        s.execute(update(Subscription).where(Subscription.user_id == user_id,
                                             Subscription.status.in_(ACTIVE_SUB_STATUSES))
                  .values(status="canceled", updated_at=now, current_period_end=now))
        s.add(Subscription(user_id=user_id, plan_id=plan.id, status="active", current_period_start=now,
                           cancel_at_period_end=False, provider=provider, created_at=now, updated_at=now))

    def assign_plan(self, user_id: int, plan_key: str, provider: str = "manual") -> None:
        """Make `plan_key` the user's only active subscription (previous ones are canceled)."""
        with session_scope(self.engine) as s:
            if s.get(User, user_id) is None:
                raise AuthError("not_found")
            self._assign(s, user_id, plan_key, self.now(), provider)
        log.info("plan assigned: user_id=%s", user_id)

    def _active_plan(self, s: Session, user_id: int) -> tuple[str, str] | None:
        now = self.now()
        row = s.execute(select(Plan.key, Plan.name).join(Subscription, Subscription.plan_id == Plan.id)
                        .where(Subscription.user_id == user_id, Subscription.status.in_(ACTIVE_SUB_STATUSES),
                               or_(Subscription.current_period_end.is_(None), Subscription.current_period_end > now),
                               Plan.is_active.is_(True))
                        .order_by(Subscription.created_at.desc(), Subscription.id.desc()).limit(1)).first()
        return tuple(row) if row else None

    def plan_for_user(self, user_id: int | None) -> tuple[str, str] | None:
        """(plan key, plan name) whose entitlements apply: the active subscription, else the default
        plan; for display only. None = no plan at all (everything denied)."""
        with session_scope(self.engine) as s:
            if user_id is not None:
                found = self._active_plan(s, user_id)
                if found:
                    return found
            key = self.anonymous_plan if user_id is None and self.anonymous_plan else default_plan_key(s)
            plan = self._plan(s, key) if key else None
            return (plan.key, plan.name) if plan else None

    def entitlements_for_user(self, user_id: int | None) -> Entitlements:
        """Active subscription's plan -> plan_entitlements; no active subscription -> default plan;
        user_id None -> anonymous_entitlements(). Missing keys deny (Entitlements.from_values)."""
        if user_id is None:
            return self.anonymous_entitlements()
        plan = self.plan_for_user(user_id)
        with session_scope(self.engine) as s:
            return for_plan(s, plan[0]) if plan else Entitlements.from_values({})

    def anonymous_entitlements(self) -> Entitlements:
        """Visitors without a session get the default (Free) plan, or SHOTS_ANON_PLAN / anonymous_plan."""
        with session_scope(self.engine) as s:
            key = self.anonymous_plan or default_plan_key(s)
            return for_plan(s, key) if key else Entitlements.from_values({})

    def list_plans(self) -> list[PlanInfo]:
        """Active plans in display order with their raw entitlement values (pricing page)."""
        with session_scope(self.engine) as s:
            plans = s.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.id)).all()
            return [PlanInfo(p.key, p.name, p.sort_order, bool(p.is_default), plan_values(s, p.key)) for p in plans]
