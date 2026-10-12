"""User-facing messages for the `error` / `reason` / `ok` codes the auth endpoints put in redirect
URLs. Templates must render these messages, never the raw query value (unknown codes map to a
generic message, so a crafted link cannot inject text)."""

from __future__ import annotations

from shots.auth import settings as S

ERRORS = {
    "invalid_credentials": "Incorrect email or password.",
    "rate_limited": "Too many attempts. Please wait a few minutes and try again.",
    "invalid_email": "Please enter a valid email address.",
    "weak_password": "Please choose a stronger password.",
    "password_mismatch": "The two passwords do not match.",
    "email_taken": "An account already exists for this email. Try signing in instead.",
    "registration_failed": "We could not create an account with these details.",
    "csrf": "Your form expired. Please try again.",
    "invalid_request": "Something went wrong. Please try again.",
    "login_required": "Please sign in to continue.",
}
WEAK_REASONS = {
    "too_short": f"Passwords must be at least {S.PASSWORD_MIN_LENGTH} characters long.",
    "too_long": f"Passwords can be at most {S.PASSWORD_MAX_LENGTH} characters long.",
    "common": "This password is too common or too easy to guess.",
    "contains_email": "Your password must not contain your email address.",
    "same": "The new password must be different from the current one.",
}
OK = {
    "password_changed": "Your password was changed. Other devices were signed out.",
    "signed_out_everywhere": "You were signed out on every device.",
}
GENERIC = ERRORS["invalid_request"]


def error_message(code: str | None, reason: str | None = None) -> str | None:
    if not code:
        return None
    if code == "weak_password" and reason in WEAK_REASONS:
        return WEAK_REASONS[reason]
    return ERRORS.get(code, GENERIC)


def ok_message(code: str | None) -> str | None:
    return OK.get(code) if code else None
