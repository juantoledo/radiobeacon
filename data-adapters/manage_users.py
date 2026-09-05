import argparse
import getpass
import logging
import sqlite3
import sys

from adapters.auth import (
    VALID_ROLES,
    create_user,
    delete_user,
    get_user_by_username,
    list_users,
    set_user_password,
    set_user_role,
)
from adapters.storage import DEFAULT_DB_PATH, get_connection

logger = logging.getLogger(__name__)


def _prompt_password() -> str:
    """Always prompted via getpass (never a plain CLI arg, which would land
    in shell history / `ps aux`), with a confirmation prompt — the
    recovery path this script exists for (a forgotten admin password)
    happens with nobody watching over your shoulder to notice a typo."""
    while True:
        password = getpass.getpass("New password: ")
        if len(password) < 8:
            print("password must be at least 8 characters", file=sys.stderr)
            continue
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("passwords did not match, try again", file=sys.stderr)
            continue
        return password


def _get_user_or_exit(conn: sqlite3.Connection, username: str):
    user = get_user_by_username(conn, username)
    if user is None:
        print(f"error: no user named {username!r}", file=sys.stderr)
        sys.exit(1)
    return user


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage radiobeacon login accounts directly against the DB — "
        "the recovery path when the web login is unusable (a forgotten admin "
        "password, or getting locked out before a second admin exists), and the "
        "CLI half of user management alongside the /users admin page."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to radiobeacon.db")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List every user")

    add_parser = subparsers.add_parser("add-user", help="Create a new user")
    add_parser.add_argument("username")
    add_parser.add_argument("--role", choices=VALID_ROLES, required=True)

    set_password_parser = subparsers.add_parser(
        "set-password", help="Reset a user's password (the account-recovery path)"
    )
    set_password_parser.add_argument("username")

    set_role_parser = subparsers.add_parser("set-role", help="Change a user's role")
    set_role_parser.add_argument("username")
    set_role_parser.add_argument("--role", choices=VALID_ROLES, required=True)

    delete_parser = subparsers.add_parser("delete-user", help="Delete a user")
    delete_parser.add_argument("username")

    args = parser.parse_args()
    conn = get_connection(args.db)  # creates + migrates the full schema on first run

    if args.command == "list":
        users = list_users(conn)
        if not users:
            print("No users defined.")
        for user in users:
            status = " (disabled)" if user.disabled else ""
            print(f"{user.username}\trole={user.role}{status}")

    elif args.command == "add-user":
        password = _prompt_password()
        try:
            create_user(conn, args.username, password, args.role, actor="cli.manage_users")
        except sqlite3.IntegrityError:
            print(f"error: username {args.username!r} is already taken", file=sys.stderr)
            sys.exit(1)
        logger.info("created user username=%s role=%s", args.username, args.role)

    elif args.command == "set-password":
        user = _get_user_or_exit(conn, args.username)
        password = _prompt_password()
        set_user_password(conn, user.id, password, actor="cli.manage_users")
        logger.info("password reset for username=%s (existing sessions invalidated)", args.username)

    elif args.command == "set-role":
        user = _get_user_or_exit(conn, args.username)
        set_user_role(conn, user.id, args.role, actor="cli.manage_users")
        logger.info("role changed for username=%s -> %s", args.username, args.role)

    elif args.command == "delete-user":
        user = _get_user_or_exit(conn, args.username)
        delete_user(conn, user.id, actor="cli.manage_users")
        logger.info("deleted user username=%s", args.username)

    conn.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    main()
