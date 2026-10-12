"""Security constants of the auth layer, in one place. Change them here (and in docs/platform.md)."""

from __future__ import annotations

from datetime import timedelta

# ----------------------------------------------------------------------------- passwords
PASSWORD_MIN_LENGTH = 12  # characters, after Unicode NFKC normalization
PASSWORD_MAX_LENGTH = 256  # bounds the hashing cost of a request (Argon2 itself has no practical limit)
# A common base word may not be "saved" by a short affix: "password1234!" -> "password" is rejected.
COMMON_AFFIX_MAX = 6
PASSWORD_MIN_DISTINCT = 4  # "aaaaaaaaaaaa", "121212121212" are rejected
# Argon2id parameters: argon2-cffi's defaults = RFC 9106 "low memory" profile (t=3, m=64 MiB, p=4).
# Hashes made with other parameters are transparently upgraded on the next successful login.
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST_KIB = 65_536
ARGON2_PARALLELISM = 4
ARGON2_MAX_CONCURRENT = 4  # hashes / verifies running at once (bounds memory: 4 x 64 MiB)

# ----------------------------------------------------------------------------- emails
EMAIL_MAX_LENGTH = 254

# ----------------------------------------------------------------------------- sessions
SESSION_COOKIE = "shots_session"
SESSION_TOKEN_BYTES = 32  # 256-bit random id (secrets.token_urlsafe); only its SHA-256 is stored
SESSION_IDLE_TIMEOUT = timedelta(days=7)  # no request for this long -> session dead
SESSION_ABSOLUTE_TIMEOUT = timedelta(days=30)  # from login, whatever the activity
ANON_SESSION_TIMEOUT = timedelta(hours=2)  # pre-login sessions (CSRF of login/register forms): idle = absolute
SESSION_TOUCH_INTERVAL = timedelta(minutes=5)  # last_seen_at is written at most this often per session
# Pre-login CSRF (login / register forms of visitors without a session): stateless signed token
# bound to a random nonce cookie; no session row is created for anonymous page views.
PRELOGIN_COOKIE = "shots_prelogin"
PRELOGIN_NONCE_BYTES = 32
PRELOGIN_CSRF_TTL = timedelta(hours=2)  # token (and cookie) lifetime
PRELOGIN_CSRF_SKEW = timedelta(minutes=5)  # tolerated clock skew for a token "issued in the future"
SESSION_PURGE_AFTER = timedelta(days=7)  # dead sessions are deleted this long after they ended
ATTEMPTS_PURGE_AFTER = timedelta(days=30)

# ----------------------------------------------------------------------------- rate limits and lockout
# Login, per account (keyed by HMAC of the normalized email, so unknown emails behave exactly like
# known ones): after LOGIN_FREE_FAILURES consecutive failures (since the last success, within
# LOCKOUT_WINDOW) the account is locked for LOCKOUT_BASE * 2^(failures - LOGIN_FREE_FAILURES),
# capped at LOCKOUT_MAX, counted from the last failure. Attempts during a lock are refused without
# checking the password and are not counted (the lock does not grow while it is being hammered).
LOGIN_FREE_FAILURES = 5
LOCKOUT_BASE = timedelta(minutes=1)
LOCKOUT_MAX = timedelta(minutes=15)  # short cap: limits how long an attacker can lock a victim out
LOCKOUT_WINDOW = timedelta(hours=24)
# Login, per client (HMAC of the client IP): sliding window over failures.
LOGIN_CLIENT_MAX_FAILURES = 20
LOGIN_CLIENT_WINDOW = timedelta(minutes=15)
# Registration (every attempt counts, success or not).
REGISTER_CLIENT_MAX = 5
REGISTER_CLIENT_WINDOW = timedelta(hours=1)
REGISTER_ACCOUNT_MAX = 3
REGISTER_ACCOUNT_WINDOW = timedelta(hours=1)
# Password change: wrong current passwords use the login backoff (kind "password", per account).

# Registration of an existing email answers `email_taken` (no email delivery exists yet, so a
# "check your inbox" flow is impossible). This leaks account existence, mitigated by the
# registration rate limits. Set to False to answer the generic `registration_failed` instead.
REVEAL_EMAIL_TAKEN = True

# ----------------------------------------------------------------------------- web
FORM_MAX_BYTES = 16_384  # auth form bodies larger than this are rejected
NEXT_MAX_LENGTH = 512
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
LOGIN_PATH = "/login"
REGISTER_PATH = "/register"
ACCOUNT_PATH = "/account"
DEFAULT_NEXT = "/"
# Environment overrides: SHOTS_COOKIE_SECURE=1 forces the Secure flag (e.g. behind a TLS proxy that
# does not forward the scheme); SHOTS_AUTH_KEY = hex/str secret for the HMAC of emails and IPs
# (default: data/auth.key, created on first use).
