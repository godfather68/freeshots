"""Password hashing (Argon2id via argon2-cffi), password policy and email normalization.

Passwords are NFKC-normalized before hashing and checking, so the same password typed on two
keyboards (composed vs decomposed accents) verifies. Nothing here logs or returns a password.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from functools import lru_cache
from importlib import resources

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from shots.auth import settings as S

_hasher = PasswordHasher(time_cost=S.ARGON2_TIME_COST, memory_cost=S.ARGON2_MEMORY_COST_KIB,
                         parallelism=S.ARGON2_PARALLELISM, type=Type.ID)
_dummy: dict[int, str] = {}
# At most ARGON2_MAX_CONCURRENT hashes / verifies run at once (each needs ARGON2_MEMORY_COST_KIB of
# RAM): a burst of logins queues here instead of exhausting memory.
_slots = threading.BoundedSemaphore(S.ARGON2_MAX_CONCURRENT)


def hasher() -> PasswordHasher:
    return _hasher


def set_hasher(h: PasswordHasher) -> None:
    """Swap the hasher (tests use cheap parameters). The dummy hash follows automatically."""
    global _hasher
    _hasher = h


def normalize_password(password: str) -> str:
    return unicodedata.normalize("NFKC", password)


def _hash(secret: str) -> str:
    with _slots:
        return _hasher.hash(secret)


def hash_password(password: str) -> str:
    return _hash(normalize_password(password))


def dummy_hash() -> str:
    """A real hash of a random secret with the current parameters: verifying against it costs the
    same as verifying a real user, so unknown emails take the same time as wrong passwords."""
    key = id(_hasher)
    if key not in _dummy:
        import secrets
        _dummy.clear()
        _dummy[key] = _hash(secrets.token_urlsafe(32))
    return _dummy[key]


def verify_password(encoded: str | None, password: str) -> bool:
    """Constant-time check (argon2's own comparison). encoded=None checks against the dummy hash
    and always returns False, at the same cost."""
    target = encoded or dummy_hash()
    try:
        with _slots:
            ok = _hasher.verify(target, normalize_password(password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return ok and encoded is not None


def needs_rehash(encoded: str) -> bool:
    try:
        return _hasher.check_needs_rehash(encoded)
    except (InvalidHashError, ValueError):
        return True


# ----------------------------------------------------------------------------- policy

@lru_cache(maxsize=1)
def common_passwords() -> frozenset[str]:
    text = resources.files("shots.auth").joinpath("common_passwords.txt").read_text(encoding="ascii")
    return frozenset(line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#"))


_AFFIX = re.compile(r"^([\d\W_]{0,%d})(.*?)([\d\W_]{0,%d})$" % (S.COMMON_AFFIX_MAX, S.COMMON_AFFIX_MAX))
_SEQUENCES = ("0123456789012345678901234567890", "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz",
              "qwertyuiopasdfghjklzxcvbnmqwertyuiop", "azertyuiopqsdfghjklmwxcvbnazertyuiop",
              "1qaz2wsx3edc4rfv5tgb6yhn7ujm8ik9ol0p", "qazwsxedcrfvtgbyhnujmikolp")


def _repeated_unit(s: str) -> bool:
    """'abcabcabcabc' / 'passpasspass': a short unit repeated."""
    n = len(s)
    return any(n % k == 0 and s[:k] * (n // k) == s for k in range(1, n // 2 + 1))


def password_problem(password: str, email: str | None = None) -> str | None:
    """None if acceptable, else a reason code: too_short | too_long | common | contains_email."""
    if not isinstance(password, str):
        return "too_short"
    pw = normalize_password(password)
    if len(pw) < S.PASSWORD_MIN_LENGTH:
        return "too_short"
    if len(pw) > S.PASSWORD_MAX_LENGTH:
        return "too_long"
    low = pw.lower()
    common = common_passwords()
    if low in common or len(set(low)) < S.PASSWORD_MIN_DISTINCT or _repeated_unit(low):
        return "common"
    m = _AFFIX.match(low)
    if m and m.group(2) and (m.group(2) in common or _repeated_unit(m.group(2))):
        return "common"
    rev = low[::-1]
    if any(low in seq or rev in seq for seq in _SEQUENCES):
        return "common"
    if email:
        local = normalize_email(email).split("@")[0]
        if len(local) >= 3 and local in low:
            return "contains_email"
    return None


# ----------------------------------------------------------------------------- emails

_LOCAL = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}$")
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def normalize_email(email: str) -> str:
    """Trim, NFKC and lower-case the whole address (one account per address, whatever the case)."""
    return unicodedata.normalize("NFKC", email or "").strip().lower()


def valid_email(email: str) -> bool:
    """Pragmatic syntax check on a normalized address: ASCII local part, dotted domain with a
    letter TLD. Deliverability can only be proven by an email confirmation (not implemented yet)."""
    if not email or len(email) > S.EMAIL_MAX_LENGTH or email.count("@") != 1:
        return False
    local, domain = email.split("@")
    if not _LOCAL.match(local) or local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    labels = domain.split(".")
    return (len(labels) >= 2 and all(_LABEL.match(lb) for lb in labels)
            and labels[-1].isalpha() and len(labels[-1]) >= 2)
