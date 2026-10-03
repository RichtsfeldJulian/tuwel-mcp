"""Command line entry point: `tuwel-mcp [serve|login|status|logout]`."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import random
import re
import sys
import webbrowser

from . import auth, config


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or (default or "")


def _yes(prompt: str, default: bool = True) -> bool:
    answer = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer.startswith("y")


def cmd_login(args: argparse.Namespace) -> int:
    if args.browser:
        return _browser_login()

    username = args.username or _ask("TU Wien username (e.g. e12345678)", auth.get_credential("username"))
    password = getpass.getpass("TU Wien password (leave empty to use stored one): ") or auth.get_credential("password")
    if not username or not password:
        print("Username and password are required.")
        return 1

    secret = args.totp_secret
    if secret is None:
        print(
            "\n2FA: enter the current 6-digit code from your authenticator app,\n"
            "or paste your TOTP secret (base32 or otpauth:// URI) to enable fully automatic token renewal."
        )
        entry = input("Code or secret (empty = use stored secret): ").strip()
        if re.fullmatch(r"\d{6}", entry):
            code = entry
        else:
            secret = entry or auth.get_credential("totp_secret")
            if not secret:
                print("No code given and no TOTP secret stored.")
                return 1
            code = auth.current_totp(secret)
    else:
        code = auth.current_totp(secret)

    print("Logging in via TU Wien SSO ...")
    try:
        session = auth.login(username, password, code)
    except auth.AuthError as exc:
        print(f"Login failed: {exc}")
        return 1
    session.save()
    print(f"Success. Token saved to {config.token_file()}")

    if not args.no_store and _yes(
        "Store username/password in the OS credential store so agents can re-login with just a 2FA code?"
    ):
        auth.set_credential("username", username)
        auth.set_credential("password", password)
        if secret and _yes("Also store the TOTP secret for fully automatic renewal (no 2FA prompts)?", False):
            auth.set_credential("totp_secret", secret)
        print("Credentials stored.")
    return cmd_status(args)


def _browser_login() -> int:
    """Fallback (e.g. for ID Austria): log in with a normal browser and paste the app-link token."""
    passport = f"{random.random() * 1000:.6f}"
    url = (
        f"{config.SITE_URL}/admin/tool/mobile/launch.php?service={config.SERVICE}"
        f"&passport={passport}&urlscheme={config.URL_SCHEME}"
    )
    print(
        "1. Open this URL in your browser (opening it now) and log in:\n"
        f"   {url}\n"
        "2. At the end the browser tries to open a 'moodlemobile://token=...' link.\n"
        "   Open DevTools (F12) > Network/Console before logging in to copy that URL\n"
        "   (it appears as a failed request / blocked navigation)."
    )
    webbrowser.open(url)
    value = input("\nPaste the moodlemobile://token=... URL: ")
    try:
        session = auth.session_from_launch_url(value, passport)
    except auth.AuthError as exc:
        print(f"Error: {exc}")
        return 1
    session.save()
    print(f"Token saved to {config.token_file()}")
    return cmd_status(argparse.Namespace())


def cmd_status(_args: argparse.Namespace) -> int:
    from . import server

    print(asyncio.run(server.tuwel_status()))
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    auth.Session.clear()
    print("Token removed.")
    if args.forget:
        for name in ("username", "password", "totp_secret"):
            auth.set_credential(name, None)
        print("Stored credentials removed.")
    return 0


def cmd_serve(_args: argparse.Namespace) -> int:
    from . import server

    server.run()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="tuwel-mcp", description="MCP server for TU Wien TUWEL (Moodle).")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Run the MCP server over stdio (default).").set_defaults(func=cmd_serve)

    p = sub.add_parser("login", help="Log in via TU Wien SSO + 2FA and store a token.")
    p.add_argument("--username")
    p.add_argument("--totp-secret", help="TOTP secret to generate the 2FA code automatically.")
    p.add_argument("--no-store", action="store_true", help="Do not offer to store credentials.")
    p.add_argument("--browser", action="store_true", help="Manual browser login (e.g. ID Austria).")
    p.set_defaults(func=cmd_login)

    sub.add_parser("status", help="Show connection status.").set_defaults(func=cmd_status)

    p = sub.add_parser("logout", help="Delete the stored token.")
    p.add_argument("--forget", action="store_true", help="Also delete stored credentials.")
    p.set_defaults(func=cmd_logout)

    args = parser.parse_args()
    if args.command is None:
        args = parser.parse_args(["serve"])
    sys.exit(args.func(args))
