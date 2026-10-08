#!/usr/bin/env python3
"""Generate auth secrets and a password hash for deployment.

    python backend/scripts/gen_auth.py

Prints lines ready to paste into the VM's /opt/multi-agent-team/.env.
Pass a password as argv[1] to hash an existing one instead of a random one.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import hash_password, new_api_key

USERNAME = "admin"


def main() -> int:
    supplied = sys.argv[1] if len(sys.argv) > 1 else None

    if supplied:
        if len(supplied) < 8:
            print("password must be at least 8 characters", file=sys.stderr)
            return 1
        password = supplied
    else:
        password = secrets.token_urlsafe(18)
        print(f"# generated password (save it now, it is not stored): {password}\n")

    print("# Append to /opt/multi-agent-team/.env on the VM")
    print(f"AUTH_USERNAME={USERNAME}")
    print(f"AUTH_SECRET={secrets.token_urlsafe(48)}")
    print(f"AUTH_PASSWORD_HASH={hash_password(password)}")
    print(f"AUTH_API_KEY={new_api_key()}")
    print("AUTH_COOKIE_SECURE=0")
    print()
    print("# Set AUTH_COOKIE_SECURE=1 once the deployment is behind HTTPS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
