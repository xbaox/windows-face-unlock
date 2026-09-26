"""Store the Windows password used by the sign-in tile -- console form, for scripted developer use.

The tray's "Windows password…" opens the dialog (presence_monitor.password_gui) in every layout; this
tool is the same save path without Tk (Stage 9, R20 / B7-11): an empty password is refused, the
password is checked ONCE with an interactive logon before anything is stored (a rejected password is
never saved), and the stored record is read back. Encrypted with DPAPI for the current Windows
account -- run it as the account that signs in.

Usage:
  python -m tools.set_password            # prompts twice; exit 0 saved, 1 not saved
  python -m tools.set_password --clear
"""
from __future__ import annotations

import argparse
import getpass
import sys

from face_service.credentials import clear_password
from presence_monitor.password_gui import account_identity, store_password_checked


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.set_password")
    ap.add_argument("--clear", action="store_true", help="remove the stored password")
    args = ap.parse_args(argv)

    if args.clear:
        clear_password()
        print("Cleared the stored password.")
        return 0

    user, domain, kind = account_identity()
    print(f"Account: {domain + chr(92) if domain else ''}{user}  ({kind})")
    pw = getpass.getpass("Windows password: ")
    if not pw:
        print("Not saved: the password is empty.", file=sys.stderr)
        return 1
    if getpass.getpass("Confirm:          ") != pw:
        print("Not saved: the passwords do not match.", file=sys.stderr)
        return 1
    level, text = store_password_checked(user, pw, domain)
    print(text, file=sys.stderr if level == "err" else sys.stdout)
    return 1 if level == "err" else 0


if __name__ == "__main__":
    raise SystemExit(main())
