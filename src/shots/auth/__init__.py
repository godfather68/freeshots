"""Accounts, sessions and entitlement-based authorization (see docs/platform.md, "Authentication").

    shots.auth.service   AuthService: register / authenticate / sessions / CSRF / rate limits / plans
    shots.auth.web       FastAPI helpers: install_auth, auth_router, current_user, require_user,
                         require_entitlement, entitlements_provider, csrf_token, verify_csrf, auth_context
    shots.auth.passwords Argon2id hashing, password policy, email normalization
    shots.auth.messages  error / ok code -> user-facing message
    shots.auth.settings  every security constant

    python -m shots.auth create-user --email you@example.com --plan premium [--admin]
"""

from shots.auth.service import AuthError, AuthService, NewSession, SessionState, UserInfo

__all__ = ["AuthError", "AuthService", "NewSession", "SessionState", "UserInfo"]
