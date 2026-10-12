"""python -m shots.auth <command>   (admin tooling; the database is SHOTS_DB or data/shots.db)

    create-user --email E --plan P [--admin]   password asked twice (never taken from the command line)
    set-plan --email E --plan P                make P the user's only active plan
    list-users                                 id, email, plan, admin, active
    revoke-sessions --email E                  sign the user out everywhere
    deactivate --email E                       block sign-in and revoke every session
    activate --email E
    purge                                      delete long-dead sessions and old login attempts
"""

from __future__ import annotations

import argparse
import getpass
import sys

from shots.auth import messages
from shots.auth.service import AuthError, AuthService
from shots.db import migrate
from shots.db.engine import db_path


def _fail(msg: str) -> None:
    sys.exit(f"error: {msg}")


def _user(svc: AuthService, email: str):
    user = svc.find_user(email)
    if user is None:
        _fail("no such user")
    return user


def _ask_password() -> str:
    first = getpass.getpass("Password: ")
    if getpass.getpass("Repeat password: ") != first:
        _fail(messages.ERRORS["password_mismatch"])
    return first


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m shots.auth", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create-user", help="create an account (password prompt)")
    c.add_argument("--email", required=True)
    c.add_argument("--plan", help="plan key (default: the default plan)")
    c.add_argument("--admin", action="store_true")
    p = sub.add_parser("set-plan", help="assign a plan")
    p.add_argument("--email", required=True)
    p.add_argument("--plan", required=True)
    sub.add_parser("list-users", help="list accounts")
    for name in ("revoke-sessions", "deactivate", "activate"):
        sub.add_parser(name).add_argument("--email", required=True)
    sub.add_parser("purge", help="delete dead sessions and old attempts")
    args = ap.parse_args(argv)

    if not db_path().exists() or not migrate.is_current():
        _fail(f"{db_path()} is missing or not migrated: run `python -m shots.db upgrade` first")
    svc = AuthService()
    try:
        if args.command == "create-user":
            user = svc.create_user(args.email, _ask_password(), plan=args.plan, is_admin=args.admin)
            print(f"created user {user.id} {user.email}" + (" (admin)" if user.is_admin else ""))
        elif args.command == "set-plan":
            svc.assign_plan(_user(svc, args.email).id, args.plan)
            print(f"plan set to {args.plan}")
        elif args.command == "list-users":
            rows = svc.list_users()
            print(f"{'id':>4}  {'email':<40} {'plan':<10} admin active")
            for u, plan in rows:
                print(f"{u.id:>4}  {u.email:<40} {plan or '-':<10} {'yes' if u.is_admin else 'no':<5} "
                      f"{'yes' if u.is_active else 'no'}")
        elif args.command == "revoke-sessions":
            print(f"revoked {svc.revoke_all(_user(svc, args.email).id)} session(s)")
        elif args.command in ("deactivate", "activate"):
            svc.set_active(_user(svc, args.email).id, args.command == "activate")
            print(f"user {args.command}d")
        elif args.command == "purge":
            print(svc.purge())
    except AuthError as exc:
        if exc.code == "invalid_plan":
            _fail("unknown or inactive plan")
        _fail(messages.error_message(exc.code, exc.reason) or exc.code)
    finally:
        svc.engine.dispose()


if __name__ == "__main__":
    main()
