#!/bin/sh
# Prints the ntfy lines for .env: an access token for Gatekeeper and bcrypt
# password hashes for the "gatekeeper" and "phone" ntfy users.
#
#   scripts/ntfy-auth.sh 'phone-app-password' >> .env
#
# The phone password is what you type into the ntfy app; it isn't stored, only
# its hash. Runs Python in a throwaway container, so only Docker is needed.
set -eu

if [ $# -ne 1 ] || [ -z "$1" ]; then
    echo "usage: $0 <phone-app-password>" >&2
    exit 2
fi

docker run --rm -e PHONE_PASSWORD="$1" python:3.11.16-slim-bookworm sh -c '
pip install -q --disable-pip-version-check --root-user-action=ignore bcrypt >/dev/null
python - <<"EOF"
import os
import secrets
import string

import bcrypt


def bcrypt_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(10, prefix=b"2a")).decode()


alphabet = string.ascii_lowercase + string.digits
token = "tk_" + "".join(secrets.choice(alphabet) for _ in range(29))
print("NTFY_GATEKEEPER_TOKEN=" + token)
# Gatekeeper logs in with the token; its password is random and never used.
print("NTFY_GATEKEEPER_PASSWORD_HASH=\x27" + bcrypt_hash(secrets.token_hex(16)) + "\x27")
print("NTFY_PHONE_PASSWORD_HASH=\x27" + bcrypt_hash(os.environ["PHONE_PASSWORD"]) + "\x27")
EOF
'
