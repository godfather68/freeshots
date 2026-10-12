"""Auth services: hashing, password policy, registration, enumeration-safe login, sessions, CSRF,
rate limiting / lockout, plans and entitlements, admin CLI."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from argon2 import PasswordHasher, Type
from sqlalchemy import delete, select, update

from shots.auth import passwords as pw
from shots.auth import settings as S
from shots.auth.service import AuthError, AuthService, csrf_for, sha256_hex
from shots.db.access import Entitlements
from shots.db.engine import session_scope
from shots.db.models import LoginAttempt, Plan, PlanEntitlement, Subscription, User, UserSession

GOOD = "violet-harbour-kettle-93"
GOOD2 = "maple-quartz-lantern-58"
FAST = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, type=Type.ID)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 9, 12, 0, 0)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture(autouse=True)
def fast_hasher():
    old = pw.hasher()
    pw.set_hasher(FAST)
    yield
    pw.set_hasher(old)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def svc(migrated_db, clock):
    s = AuthService(migrated_db, secret=b"k" * 32, clock=clock)
    yield s
    s.engine.dispose()


def rows(svc, model):
    with session_scope(svc.engine) as s:
        return s.scalars(select(model)).all()


# ----------------------------------------------------------------------------- hashing

def test_hash_is_argon2id_and_verifies():
    h = pw.hash_password(GOOD)
    assert h.startswith("$argon2id$") and GOOD not in h
    assert pw.verify_password(h, GOOD)
    assert not pw.verify_password(h, GOOD + "x")
    assert not pw.verify_password("not-a-hash", GOOD)
    assert pw.hash_password(GOOD) != h  # salted


def test_default_parameters_are_rfc9106_low_memory():
    pw.set_hasher(PasswordHasher(time_cost=S.ARGON2_TIME_COST, memory_cost=S.ARGON2_MEMORY_COST_KIB,
                                 parallelism=S.ARGON2_PARALLELISM, type=Type.ID))
    h = pw.hash_password(GOOD)
    assert "$argon2id$v=19$m=65536,t=3,p=4$" in h


def test_unicode_normalization():
    composed, decomposed = "café-harbour-kettle", "café-harbour-kettle"
    assert pw.verify_password(pw.hash_password(composed), decomposed)


def test_dummy_hash_verification_always_false_but_runs_argon2():
    calls = []

    class Spy(PasswordHasher):
        def verify(self, h, p):
            calls.append(h)
            return super().verify(h, p)

    pw.set_hasher(Spy(time_cost=1, memory_cost=8, parallelism=1, type=Type.ID))
    assert pw.verify_password(None, GOOD) is False
    assert len(calls) == 1 and calls[0].startswith("$argon2id$")


# ----------------------------------------------------------------------------- policy

@pytest.mark.parametrize("password,reason", [
    ("short-pass1", "too_short"),
    ("x" * (S.PASSWORD_MAX_LENGTH + 1), "too_long"),
    ("password1234", "common"),
    ("Password1234!", "common"),
    ("qwertyuiop12", "common"),
    ("1234567890123", "common"),
    ("abcdefghijklm", "common"),
    ("abcabcabcabc", "common"),
    ("aaaaaaaaaaaaaaa", "common"),
    ("121212121212", "common"),
    ("!!football2024", "common"),
    ("johnsmith-blue-sky", "contains_email"),
])
def test_password_policy_rejects(password, reason):
    assert pw.password_problem(password, "JohnSmith@example.com") == reason


@pytest.mark.parametrize("password", [GOOD, "correct horse battery staple", "Tr0ub4dor&3xyz", "x" * 3 + "yz9!Kq2w7Lm"])
def test_password_policy_accepts(password):
    assert pw.password_problem(password, "someone@example.com") is None


def test_common_list_is_bundled():
    common = pw.common_passwords()
    assert len(common) > 9000 and "password" in common and "qwerty" in common


def test_min_length_constant():
    assert S.PASSWORD_MIN_LENGTH == 12
    assert pw.password_problem("k7#Lq9!vR2x") == "too_short"  # 11 chars
    assert pw.password_problem("k7#Lq9!vR2xZ") is None  # 12 chars


@pytest.mark.parametrize("raw,norm,ok", [
    ("  Alice@Example.COM ", "alice@example.com", True),
    ("bob.smith+tag@mail.example.co.uk", "bob.smith+tag@mail.example.co.uk", True),
    ("no-at-sign.example.com", None, False),
    ("two@@example.com", None, False),
    ("a@localhost", None, False),
    ("a@exa mple.com", None, False),
    (".a@example.com", None, False),
    ("a..b@example.com", None, False),
    ("a@-example.com", None, False),
    ("a@example.c0m", None, False),
])
def test_email_normalization_and_validation(raw, norm, ok):
    n = pw.normalize_email(raw)
    if norm:
        assert n == norm
    assert pw.valid_email(n) is ok


# ----------------------------------------------------------------------------- registration

def test_register_creates_user_with_default_plan(svc):
    u = svc.register(" New@Example.com ", GOOD, "1.2.3.4")
    assert u.email == "new@example.com" and not u.is_admin and u.is_active
    (row,) = rows(svc, User)
    assert row.password_hash.startswith("$argon2id$") and GOOD not in row.password_hash
    assert svc.plan_for_user(u.id)[0] == "free"
    (sub,) = rows(svc, Subscription)
    assert sub.status == "active" and sub.provider == "manual"
    assert svc.entitlements_for_user(u.id).max_days_ahead == 3


@pytest.mark.parametrize("email,password,confirm,code,reason", [
    ("bad-email", GOOD, None, "invalid_email", None),
    ("a@example.com", "short", None, "weak_password", "too_short"),
    ("a@example.com", "password1234", None, "weak_password", "common"),
    ("a@example.com", GOOD, GOOD2, "password_mismatch", None),
])
def test_register_validation(svc, email, password, confirm, code, reason):
    with pytest.raises(AuthError) as e:
        svc.register(email, password, "1.2.3.4", password_confirm=confirm)
    assert (e.value.code, e.value.reason) == (code, reason)
    assert rows(svc, User) == []


def test_register_duplicate_email_case_insensitive(svc, monkeypatch):
    svc.register("dup@example.com", GOOD)
    with pytest.raises(AuthError) as e:
        svc.register("DUP@example.com", GOOD2)
    assert e.value.code == "email_taken"
    monkeypatch.setattr(S, "REVEAL_EMAIL_TAKEN", False)
    with pytest.raises(AuthError) as e:
        svc.register("dup@example.com", GOOD2)
    assert e.value.code == "registration_failed"


def test_register_hashes_before_existence_check(svc, monkeypatch):
    """A taken email costs the same Argon2 work as a new one (no timing oracle)."""
    svc.register("t@example.com", GOOD)
    calls = []
    real = pw.hash_password
    monkeypatch.setattr(pw, "hash_password", lambda p: calls.append(1) or real(p))
    with pytest.raises(AuthError):
        svc.register("t@example.com", GOOD2)
    assert calls == [1]


# ----------------------------------------------------------------------------- authentication

def test_authenticate_generic_errors(svc):
    u = svc.register("a@example.com", GOOD)
    assert svc.authenticate("A@EXAMPLE.com", GOOD).id == u.id
    errors = []
    for email, password in [("a@example.com", "wrong-password-xx"), ("nobody@example.com", GOOD), ("", "")]:
        with pytest.raises(AuthError) as e:
            svc.authenticate(email, password)
        errors.append((e.value.code, str(e.value)))
    svc.set_active(u.id, False)
    with pytest.raises(AuthError) as e:
        svc.authenticate("a@example.com", GOOD)
    errors.append((e.value.code, str(e.value)))
    assert set(errors) == {("invalid_credentials", "invalid_credentials")}


def test_authenticate_unknown_user_runs_one_verify(svc, monkeypatch):
    svc.register("a@example.com", GOOD)
    seen = []
    real = pw.verify_password
    monkeypatch.setattr(pw, "verify_password", lambda h, p: seen.append(h) or real(h, p))
    for email in ("a@example.com", "ghost@example.com"):
        with pytest.raises(AuthError):
            svc.authenticate(email, "wrong-password-xx")
    assert len(seen) == 2 and seen[0] is not None and seen[1] is None  # None -> dummy hash, same cost


def test_login_upgrades_outdated_hash(svc):
    u = svc.register("a@example.com", GOOD)
    pw.set_hasher(PasswordHasher(time_cost=2, memory_cost=16, parallelism=1, type=Type.ID))
    svc.authenticate("a@example.com", GOOD)
    (row,) = rows(svc, User)
    assert "m=16,t=2" in row.password_hash
    assert svc.authenticate("a@example.com", GOOD).id == u.id


def test_logs_never_contain_secrets(svc, caplog):
    caplog.set_level("DEBUG")
    svc.register("secret.person@example.com", GOOD, "9.9.9.9")
    _, new = svc.login("secret.person@example.com", GOOD, "9.9.9.9")
    with pytest.raises(AuthError):
        svc.login("secret.person@example.com", "wrong-password-xx")
    text = caplog.text + repr(new)
    for secret in (GOOD, "secret.person", new.token, new.csrf, "9.9.9.9"):
        assert secret not in text


# ----------------------------------------------------------------------------- sessions

def test_session_stores_only_hash(svc):
    u = svc.register("a@example.com", GOOD)
    new = svc.create_session(u.id, "1.2.3.4", "UA")
    assert len(new.token) >= 43  # 32 random bytes, base64url
    (row,) = rows(svc, UserSession)
    assert row.id_hash == sha256_hex(new.token) and new.token not in (row.id_hash, row.csrf_hash)
    assert row.csrf_hash == sha256_hex(new.csrf) and row.ip_hash and "1.2.3.4" not in row.ip_hash
    assert svc.validate(new.token).user.id == u.id
    assert svc.validate("forged-token") is None and svc.validate(None) is None and svc.validate("x" * 500) is None


def test_session_ids_are_unique_random(svc):
    assert S.SESSION_TOKEN_BYTES * 8 == 256
    tokens = {svc.create_session(None).token for _ in range(50)}
    assert len(tokens) == 50


def test_login_rotates_session(svc):
    svc.register("a@example.com", GOOD)
    anon = svc.create_session(None)
    assert svc.validate(anon.token).user is None
    user, new = svc.login("a@example.com", GOOD, old_token=anon.token)
    assert new.token != anon.token and new.csrf != anon.csrf
    assert svc.validate(anon.token) is None  # the pre-login id is dead
    assert svc.validate(new.token).user.id == user.id


def test_idle_expiry_and_touch(svc, clock):
    u = svc.register("a@example.com", GOOD)
    new = svc.create_session(u.id)
    clock.advance(minutes=1)
    assert svc.validate(new.token).last_seen_at == clock.t - timedelta(minutes=1)  # not touched yet
    clock.advance(minutes=S.SESSION_TOUCH_INTERVAL.seconds // 60)
    assert svc.validate(new.token).last_seen_at == clock.t  # touched
    clock.advance(seconds=S.SESSION_IDLE_TIMEOUT.total_seconds() - 1)
    assert svc.validate(new.token) is not None
    clock.advance(seconds=S.SESSION_IDLE_TIMEOUT.total_seconds())
    assert svc.validate(new.token) is None


def test_absolute_expiry_despite_activity(svc, clock):
    u = svc.register("a@example.com", GOOD)
    new = svc.create_session(u.id)
    for _ in range(S.SESSION_ABSOLUTE_TIMEOUT.days):
        clock.advance(days=1)
        state = svc.validate(new.token)
    assert state is None
    clock.t -= timedelta(seconds=1)
    assert svc.validate(new.token) is not None


def test_anonymous_session_is_short_lived(svc, clock):
    anon = svc.create_session(None)
    assert anon.max_age == S.ANON_SESSION_TIMEOUT.total_seconds()
    clock.advance(seconds=S.ANON_SESSION_TIMEOUT.total_seconds())
    assert svc.validate(anon.token) is None


def test_revoke_and_revoke_all(svc):
    u = svc.register("a@example.com", GOOD)
    a, b, c = (svc.create_session(u.id) for _ in range(3))
    svc.revoke(a.token)
    assert svc.validate(a.token) is None and svc.validate(b.token) is not None
    assert svc.revoke_all(u.id, except_token=c.token) == 1
    assert svc.validate(b.token) is None and svc.validate(c.token) is not None
    svc.revoke("unknown")  # ignored


def test_deactivated_user_sessions_die(svc):
    u = svc.register("a@example.com", GOOD)
    new = svc.create_session(u.id)
    svc.set_active(u.id, False)
    assert svc.validate(new.token) is None
    svc.set_active(u.id, True)
    assert svc.validate(new.token) is None  # revoked, not just hidden


def test_change_password(svc):
    u = svc.register("a@example.com", GOOD)
    other, mine = svc.create_session(u.id), svc.create_session(u.id)
    with pytest.raises(AuthError) as e:
        svc.change_password(u.id, "wrong-password-xx", GOOD2)
    assert e.value.code == "invalid_credentials"
    for new, confirm, code, reason in [("short", None, "weak_password", "too_short"),
                                       (GOOD, None, "weak_password", "same"),
                                       (GOOD2, "other-thing-123", "password_mismatch", None)]:
        with pytest.raises(AuthError) as e:
            svc.change_password(u.id, GOOD, new, confirm)
        assert (e.value.code, e.value.reason) == (code, reason)
    fresh = svc.change_password(u.id, GOOD, GOOD2, GOOD2)
    assert svc.validate(other.token) is None and svc.validate(mine.token) is None
    assert svc.validate(fresh.token).user.id == u.id
    with pytest.raises(AuthError):
        svc.authenticate("a@example.com", GOOD)
    assert svc.authenticate("a@example.com", GOOD2).id == u.id


def test_change_password_backoff(svc, clock):
    u = svc.register("a@example.com", GOOD)
    for _ in range(S.LOGIN_FREE_FAILURES):
        with pytest.raises(AuthError):
            svc.change_password(u.id, "wrong-password-xx", GOOD2)
    with pytest.raises(AuthError) as e:
        svc.change_password(u.id, GOOD, GOOD2)
    assert e.value.code == "rate_limited"


# ----------------------------------------------------------------------------- CSRF

def test_csrf(svc):
    u = svc.register("a@example.com", GOOD)
    a, b = svc.create_session(u.id), svc.create_session(None)
    assert a.csrf == csrf_for(a.token) and svc.check_csrf(a.token, a.csrf)
    assert svc.check_csrf(b.token, b.csrf)
    assert not svc.check_csrf(a.token, b.csrf)  # token of another session
    assert not svc.check_csrf(a.token, "")
    assert not svc.check_csrf(a.token, None)
    assert not svc.check_csrf(None, a.csrf)
    assert not svc.check_csrf("forged", csrf_for("forged"))  # no live session
    svc.revoke(a.token)
    assert not svc.check_csrf(a.token, a.csrf)


# ----------------------------------------------------------------------------- rate limits / lockout

def _fail_login(svc, email, client=None, n=1):
    for _ in range(n):
        with pytest.raises(AuthError) as e:
            svc.authenticate(email, "wrong-password-xx", client)
    return e.value.code


@pytest.mark.parametrize("email", ["a@example.com", "ghost@example.com"])
def test_account_lockout_with_backoff(svc, clock, email):
    """Identical for known and unknown emails (no enumeration through the lockout)."""
    svc.register("a@example.com", GOOD)
    assert _fail_login(svc, email, n=S.LOGIN_FREE_FAILURES) == "invalid_credentials"
    with pytest.raises(AuthError) as e:
        svc.authenticate(email, GOOD)  # even the right password is refused while locked
    assert e.value.code == "rate_limited"
    n_attempts = len(rows(svc, LoginAttempt))
    clock.advance(seconds=S.LOCKOUT_BASE.total_seconds() - 1)
    assert _fail_login(svc, email) == "rate_limited"
    assert len(rows(svc, LoginAttempt)) == n_attempts  # attempts during a lock are not counted
    clock.advance(seconds=1)
    assert _fail_login(svc, email) == "invalid_credentials"  # 6th failure -> 2 minutes
    clock.advance(seconds=2 * S.LOCKOUT_BASE.total_seconds() - 1)
    assert _fail_login(svc, email) == "rate_limited"
    clock.advance(seconds=1)
    if email == "a@example.com":
        assert svc.authenticate(email, GOOD)  # success resets the counter
        assert _fail_login(svc, email) == "invalid_credentials"


def test_lockout_is_capped(svc, clock):
    acct = svc.account_key("x@example.com")
    with session_scope(svc.engine) as s:
        for i in range(30):
            s.add(LoginAttempt(kind="login", account_hash=acct, success=False, at=clock.t - timedelta(seconds=30 - i)))
    with session_scope(svc.engine) as s:
        assert svc.lockout_until(s, "login", acct, clock.t) == clock.t - timedelta(seconds=1) + S.LOCKOUT_MAX
    clock.advance(seconds=S.LOCKOUT_MAX.total_seconds())
    assert _fail_login(svc, "x@example.com") == "invalid_credentials"


def test_lockout_window(svc, clock):
    _fail_login(svc, "x@example.com", n=S.LOGIN_FREE_FAILURES)
    clock.advance(seconds=S.LOCKOUT_WINDOW.total_seconds() + 1)
    assert _fail_login(svc, "x@example.com") == "invalid_credentials"  # old failures forgotten


def test_client_rate_limit(svc, clock):
    for i in range(S.LOGIN_CLIENT_MAX_FAILURES):
        _fail_login(svc, f"user{i}@example.com", client="6.6.6.6")
    svc.register("a@example.com", GOOD)
    with pytest.raises(AuthError) as e:
        svc.authenticate("a@example.com", GOOD, "6.6.6.6")
    assert e.value.code == "rate_limited"
    assert svc.authenticate("a@example.com", GOOD, "7.7.7.7")  # other clients unaffected
    clock.advance(seconds=S.LOGIN_CLIENT_WINDOW.total_seconds() + 1)
    assert svc.authenticate("a@example.com", GOOD, "6.6.6.6")


def test_register_rate_limits(svc, clock):
    for i in range(S.REGISTER_CLIENT_MAX):
        svc.register(f"u{i}@example.com", GOOD, "5.5.5.5")
    with pytest.raises(AuthError) as e:
        svc.register("late@example.com", GOOD, "5.5.5.5")
    assert e.value.code == "rate_limited"
    for i in range(S.REGISTER_ACCOUNT_MAX):  # per account, across clients (failures count too)
        with pytest.raises(AuthError):
            svc.register("u0@example.com", GOOD, f"10.0.0.{i}")
    with pytest.raises(AuthError) as e:
        svc.register("u0@example.com", GOOD, "10.0.1.1")
    assert e.value.code == "rate_limited"
    clock.advance(seconds=S.REGISTER_CLIENT_WINDOW.total_seconds() + 1)
    assert svc.register("late@example.com", GOOD, "5.5.5.5")


def test_attempts_store_no_raw_identifiers(svc):
    _fail_login(svc, "who@example.com", client="8.8.4.4")
    (a,) = rows(svc, LoginAttempt)
    assert "who" not in a.account_hash and "8.8" not in a.client_hash and len(a.account_hash) == 64
    other = AuthService(svc.engine, secret=b"z" * 32)
    assert other.account_key("who@example.com") != a.account_hash  # keyed, not a plain hash


def test_secret_file_created_next_to_db(migrated_db, monkeypatch):
    monkeypatch.delenv("SHOTS_AUTH_KEY", raising=False)
    s = AuthService(migrated_db)
    k1 = s.account_key("a@example.com")
    assert (migrated_db.parent / "auth.key").exists()
    s2 = AuthService(migrated_db)
    assert s2.account_key("a@example.com") == k1
    s.engine.dispose()
    s2.engine.dispose()


def test_purge(svc, clock):
    u = svc.register("a@example.com", GOOD)
    old = svc.create_session(u.id)
    svc.revoke(old.token)
    live = svc.create_session(u.id)
    _fail_login(svc, "x@example.com")
    clock.advance(days=31)
    svc.validate(live.token)
    out = svc.purge()
    assert out["attempts"] >= 2 and out["sessions"] >= 1
    assert {r.id_hash for r in rows(svc, UserSession)} <= {sha256_hex(live.token)}


# ----------------------------------------------------------------------------- plans / entitlements

def test_assign_plan_and_entitlements(svc):
    u = svc.register("a@example.com", GOOD)
    free = svc.entitlements_for_user(u.id)
    assert not free.can_view_all_leagues and "Spain La Liga" in free.allowed_leagues and not free.team_predictions
    svc.assign_plan(u.id, "pro")
    pro = svc.entitlements_for_user(u.id)
    assert pro.can_view_all_leagues and pro.max_days_ahead == 30 and pro.team_predictions and not pro.export
    svc.assign_plan(u.id, "premium")
    prem = svc.entitlements_for_user(u.id)
    assert prem.max_days_ahead is None and prem.export and prem.track_detail
    subs = rows(svc, Subscription)
    assert sorted(s.status for s in subs) == ["active", "canceled", "canceled"]
    with pytest.raises(AuthError) as e:
        svc.assign_plan(u.id, "no-such-plan")
    assert e.value.code == "invalid_plan"
    assert svc.plan_for_user(u.id) == ("premium", "Premium")


def test_missing_entitlement_key_denies(svc):
    u = svc.register("a@example.com", GOOD)
    svc.assign_plan(u.id, "premium")
    with session_scope(svc.engine) as s:
        pid = s.scalar(select(Plan.id).where(Plan.key == "premium"))
        s.execute(delete(PlanEntitlement).where(PlanEntitlement.plan_id == pid,
                                                PlanEntitlement.key.in_(["export", "max_days_ahead"])))
    e = svc.entitlements_for_user(u.id)
    assert e.export is False and e.max_days_ahead == 0 and e.track_detail is True


def test_expired_or_inactive_plan_falls_back_to_default(svc, clock):
    u = svc.register("a@example.com", GOOD)
    svc.assign_plan(u.id, "pro")
    with session_scope(svc.engine) as s:
        s.execute(update(Subscription).where(Subscription.status == "active")
                  .values(current_period_end=clock.t + timedelta(days=1)))
    assert svc.entitlements_for_user(u.id).can_view_all_leagues
    clock.advance(days=2)
    assert not svc.entitlements_for_user(u.id).can_view_all_leagues  # period over -> default plan
    with session_scope(svc.engine) as s:
        s.execute(update(Plan).where(Plan.key == "free").values(is_active=False))
    assert svc.entitlements_for_user(u.id) == Entitlements.from_values({})  # nothing left: deny all
    assert svc.anonymous_entitlements() == Entitlements.from_values({})


def test_anonymous_entitlements(svc, migrated_db):
    anon = svc.anonymous_entitlements()
    assert anon == svc.entitlements_for_user(None)
    assert not anon.can_view_all_leagues and anon.max_days_ahead == 3  # the default (Free) plan
    assert svc.plan_for_user(None) == ("free", "Free")
    other = AuthService(migrated_db, secret=b"k" * 32, anonymous_plan="pro")
    assert other.anonymous_entitlements().can_view_all_leagues
    nothing = AuthService(migrated_db, secret=b"k" * 32, anonymous_plan="does-not-exist")
    assert nothing.anonymous_entitlements() == Entitlements.from_values({})
    for x in (other, nothing):
        x.engine.dispose()


def test_list_plans(svc):
    plans = svc.list_plans()
    assert [p.key for p in plans] == ["free", "pro", "premium"]
    assert plans[0].is_default and plans[2].entitlements["export"] is True


def test_admin_create_user(svc):
    u = svc.create_user("boss@example.com", GOOD, plan="premium", is_admin=True)
    assert u.is_admin and svc.plan_for_user(u.id)[0] == "premium"
    with pytest.raises(AuthError) as e:
        svc.create_user("x@example.com", GOOD, plan="nope")
    assert e.value.code == "invalid_plan" and svc.find_user("x@example.com") is None
    with pytest.raises(AuthError):
        svc.create_user("y@example.com", "weak")


# ----------------------------------------------------------------------------- CLI

@pytest.fixture
def cli_db(migrated_db, monkeypatch):
    monkeypatch.setenv("SHOTS_DB", str(migrated_db))
    monkeypatch.setenv("SHOTS_AUTH_KEY", "test-key")
    return migrated_db


def test_cli(cli_db, monkeypatch, capsys):
    from shots.auth.__main__ import main

    answers = iter([GOOD, GOOD])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    main(["create-user", "--email", "Admin@Example.com", "--plan", "premium", "--admin"])
    assert "created user 1 admin@example.com (admin)" in capsys.readouterr().out
    main(["set-plan", "--email", "admin@example.com", "--plan", "pro"])
    main(["list-users"])
    out = capsys.readouterr().out
    assert "admin@example.com" in out and "pro" in out and GOOD not in out
    svc = AuthService(cli_db)
    tok = svc.create_session(1).token
    main(["revoke-sessions", "--email", "admin@example.com"])
    assert "revoked 1" in capsys.readouterr().out and svc.validate(tok) is None
    main(["deactivate", "--email", "admin@example.com"])
    assert svc.find_user("admin@example.com").is_active is False
    main(["activate", "--email", "admin@example.com"])
    assert svc.find_user("admin@example.com").is_active is True
    svc.engine.dispose()

    with pytest.raises(SystemExit) as e:
        main(["set-plan", "--email", "nobody@example.com", "--plan", "pro"])
    assert "no such user" in str(e.value)
    answers = iter(["password1234", "password1234"])
    with pytest.raises(SystemExit) as e:
        main(["create-user", "--email", "weak@example.com"])
    assert "too common" in str(e.value)
    answers = iter([GOOD, GOOD2])
    with pytest.raises(SystemExit) as e:
        main(["create-user", "--email", "mm@example.com"])
    assert "do not match" in str(e.value)


def test_cli_never_takes_password_from_argv(cli_db):
    from shots.auth.__main__ import main

    with pytest.raises(SystemExit):
        main(["create-user", "--email", "a@example.com", "--password", GOOD])


def test_cli_requires_migrated_db(tmp_path, monkeypatch):
    from shots.auth.__main__ import main

    monkeypatch.setenv("SHOTS_DB", str(tmp_path / "missing.db"))
    with pytest.raises(SystemExit) as e:
        main(["list-users"])
    assert "upgrade" in str(e.value)
