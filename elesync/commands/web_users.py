"""Web-dashboard user administration CLI commands."""

from __future__ import annotations

import getpass
import os


def get_web_password(confirm: bool) -> str:
    """Read a dashboard password from the environment or a hidden prompt."""
    from .. import cli

    password = os.environ.get("ELESYNC_WEB_PASSWORD")
    if password:
        return password

    password = getpass.getpass("Password: ")
    if not password:
        cli._err("Password cannot be empty.")
        raise SystemExit(1)

    if confirm and password != getpass.getpass("Confirm password: "):
        cli._err("Passwords do not match.")
        raise SystemExit(1)

    return password


def _user_manager(args):
    from .. import cli, web_auth
    from ..web import web_auth_dir

    vault = cli._resolve_vault(args)
    return vault, web_auth.UserManager(web_auth_dir(vault))


def cmd_web_adduser(args) -> int:
    from .. import cli

    vault = cli._resolve_vault(args)
    if not (vault / "notes").exists():
        cli._err(f"No EleSync vault found at {vault}")
        return 1

    password = cli._get_web_password(confirm=True)
    _, manager = _user_manager(args)
    is_first_user = len(manager.list_users()) == 0
    effective_role = "viewer" if args.guest else args.role

    try:
        manager.add_user(
            args.username,
            password,
            role=args.role,
            guest=args.guest,
        )
    except ValueError as error:
        cli._err(error)
        return 1

    print(f"✅  User '{args.username}' added ({effective_role}).")
    if is_first_user:
        print("🔒  This vault's web dashboard now requires login.")
        print("    Sign in at the /login page when you run `ele web`.")

    return 0


def cmd_web_removeuser(args) -> int:
    from .. import cli

    _, manager = _user_manager(args)

    try:
        manager.remove_user(args.username)
    except ValueError as error:
        cli._err(error)
        return 1

    print(f"✅  User '{args.username}' removed.")
    if len(manager.list_users()) == 0:
        print("🔓  No users left — this vault's web dashboard is now open, no login required.")

    return 0


def cmd_web_listusers(args) -> int:
    _, manager = _user_manager(args)
    users = manager.list_users()

    if not users:
        print("No users configured — this vault's web dashboard is open, no login required.")
        print("Use `ele web adduser <name>` to require login.")
        return 0

    print(f"{'Username':<20} {'Role':<10} Created")
    print("-" * 55)
    for user in users:
        created = (user.get("created_at") or "")[:19].replace("T", " ")
        print(f"{user['username']:<20} {user['role']:<10} {created}")

    return 0
