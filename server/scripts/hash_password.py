#!/usr/bin/env python3
"""Bcrypt hash generálása az admin jelszóhoz.

Használat:
    python scripts/hash_password.py            # bekéri a jelszót (nem látszik)
    python scripts/hash_password.py "jelszo"   # egy soros, nem interaktív

A kiírt hash-t az ADMIN_PASSWORD_HASH környezeti változóba kell beírni
(Railway-en: a service Variables fülén).
"""

from __future__ import annotations

import getpass
import sys

import bcrypt


def main() -> int:
    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        password = getpass.getpass("Admin jelszó: ")
        if password != getpass.getpass("Még egyszer: "):
            print("A két jelszó nem egyezik.", file=sys.stderr)
            return 1

    if len(password) < 8:
        print("Legalább 8 karakter legyen.", file=sys.stderr)
        return 1

    digest = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
    print()
    print("ADMIN_PASSWORD_HASH=" + digest)
    print()
    print("Ezt állítsd be környezeti változóként. A jelszót sehol ne tárold nyíltan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
