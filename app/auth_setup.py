"""Interactively configure the dashboard password hash without echoing it."""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path

from app.security import hash_password


ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
VARIABLE = "DASHBOARD_PASSWORD_HASH"


def _write_hash_only(password_hash: str) -> None:
    if not ENV_PATH.is_file():
        raise FileNotFoundError
    text = ENV_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    matching = [
        index for index, line in enumerate(lines)
        if line.strip().removeprefix("export ").partition("=")[0].strip() == VARIABLE
    ]
    if len(matching) > 1:
        raise ValueError("Duplicate dashboard password configuration entries.")
    new_line = f"{VARIABLE}={password_hash}"
    if matching:
        lines[matching[0]] = new_line
    else:
        lines.append(new_line)
    ending = "\r\n" if "\r\n" in text else "\n"
    ENV_PATH.write_text(ending.join(lines) + ending, encoding="utf-8", newline="")


def _restrict_windows_acl() -> bool:
    if os.name != "nt":
        return False
    identity = subprocess.run(
        ["whoami"], capture_output=True, text=True, check=False, timeout=5
    )
    account = identity.stdout.strip()
    if identity.returncode != 0 or not account:
        return False
    result = subprocess.run(
        [
            "icacls",
            str(ENV_PATH),
            "/inheritance:r",
            "/grant:r",
            f"{account}:(F)",
            "*S-1-5-18:(F)",
            "*S-1-5-32-544:(F)",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result.returncode == 0


def main() -> int:
    if not ENV_PATH.is_file():
        print("The existing .env file is missing. Transfer it securely before configuring dashboard access.")
        return 2
    password = getpass.getpass("New dashboard password (at least 16 characters): ")
    confirmation = getpass.getpass("Confirm dashboard password: ")
    if len(password) < 16 or len(password.encode("utf-8")) > 1024 or password != confirmation:
        print("Password confirmation or length check failed; configuration was not changed.")
        return 2
    if not _restrict_windows_acl():
        print("Windows file permissions could not be restricted. Configuration was not changed; do not activate network access until .env is restricted to your account and system administrators.")
        return 3
    try:
        password_hash = hash_password(password)
        _write_hash_only(password_hash)
    except (OSError, ValueError, MemoryError):
        print("Dashboard password configuration could not be updated; no secret was displayed.")
        return 2
    print("Dashboard password hash saved and .env permissions restricted. The password and hash were not displayed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
