#!/usr/bin/env bash
# Create a private staging .env from the reviewed template. Secret values are never printed.
set -euo pipefail
cd "$(dirname "$0")/../.."

if [[ -e .env ]]; then
  echo "Refusing to replace existing .env; back it up and edit it explicitly." >&2
  exit 1
fi
python3 - <<'PY'
from pathlib import Path
import os
import secrets
from urllib.parse import quote

template = Path('.env.staging.example').read_text(encoding='utf-8')
values = {
    'POSTGRES_PASSWORD': secrets.token_urlsafe(32),
    'SAP_BRIM_DB_PASSWORD': secrets.token_urlsafe(32),
    'JWT_SECRET': secrets.token_urlsafe(48),
    'ADMIN_API_KEY': secrets.token_urlsafe(32),
    'SUPER_ADMIN_PASSWORD': secrets.token_urlsafe(24),
    # docker-compose.prod.yml refuses to start the redis service without it. Hex keeps it safe inside
    # the redis:// URL and the redis-server command line.
    'REDIS_PASSWORD': secrets.token_hex(32),
}
lines = []
for line in template.splitlines():
    key, sep, _ = line.partition('=')
    if sep and key in values:
        line = f'{key}={values[key]}'
    elif key == 'DATABASE_URL' and sep:
        # Passwords are URL encoded so reserved characters cannot corrupt the connection URL.
        line = ('DATABASE_URL=postgresql://sap_brim_ai:' + quote(values['SAP_BRIM_DB_PASSWORD'], safe='')
                + '@postgres:5432/sap_brim_ai')
    lines.append(line)
content = '\n'.join(lines) + '\n'
fd = os.open('.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
    stream.write(content)
Path('.admin-initial-password').write_text(values['SUPER_ADMIN_PASSWORD'] + '\n', encoding='utf-8')
os.chmod('.admin-initial-password', 0o600)
print('Created .env and .admin-initial-password with fresh secrets (values withheld). Set APP_DOMAIN, ALLOWED_ORIGINS, SUPER_ADMIN_EMAIL, provider key, and staging basic-auth hash.')
PY
chmod 600 .env
