# Prompt Manage

A small **personal prompt library**: Flask + SQLite + Jinja, no frontend framework, one container.

Prompts you rely on deserve the same treatment as code — findable, reusable, and with a visible
history of every change. That is all this project does.

- **Library** — name, source, tags, notes, accent colour; grid or list view
- **Version history** — every save is a version; compare, roll back, auto-prune beyond a limit
- **Instant search** — as you type, across names, tags, sources, notes and body text
- **Markdown reading** — long prompts render as Markdown, with a one-click raw view
- **Command palette** — `⌘K` / `Ctrl+K` to search and jump; `/` focuses the list search
- **Access control** — open, or one site-wide password
- **Backups** — export/import JSON; a snapshot is written before every import
- **Bilingual** (Chinese/English) with light, dark and system themes

> This repository is a trimmed rewrite of [zhuchenyu2008/prompt-manage](https://github.com/zhuchenyu2008/prompt-manage):
> per-prompt passwords, cover images, favourites (folded into pinning), copy counters and CSV
> import/export are gone, and the backend is split into a `promptmanage/` package.
> Upgrades migrate existing data automatically — see [Upgrading](#upgrading).

---

## Deploy with Docker Compose

```bash
mkdir -p prompt-manage && cd prompt-manage
curl -fsSL https://raw.githubusercontent.com/GZC888/prompt-manage/main/docker-compose.yml -o docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/GZC888/prompt-manage/main/.env.example -o .env

echo "SECRET_KEY=$(openssl rand -hex 32)" >> .env
echo "BOOTSTRAP_TOKEN=$(openssl rand -hex 32)" >> .env
# then pin PROMPT_MANAGE_IMAGE to a concrete tag, e.g. :sha-abc1234

docker compose up -d
```

Open `http://127.0.0.1:3501/setup`, enter the `BOOTSTRAP_TOKEN` and choose an access password.
Afterwards you can clear `BOOTSTRAP_TOKEN` and restart; it only exists to claim a fresh database.

The container binds to `127.0.0.1` by default. To publish it, put a reverse proxy that terminates
HTTPS in front — do not expose the container port directly.

### Build from source

```bash
git clone https://github.com/GZC888/prompt-manage.git && cd prompt-manage
docker build -t prompt-manage:local .
PROMPT_MANAGE_IMAGE=prompt-manage:local docker compose up -d
```

### Dokploy

Create a **Docker Compose** application pointing at this repository with `docker-compose.yml`,
set `SECRET_KEY` / `BOOTSTRAP_TOKEN` in the panel, and mount `/app/data` on a persistent volume.
Set `TRUST_PROXY_HEADERS=true` when Dokploy's Traefik terminates HTTPS for you.

> ⚠️ Give the app its own volume (e.g. `prompt-manage-data`). Sharing a volume literally named
> `data` with other applications mixes unrelated files together and makes backups risky.

---

## Configuration

Full list in [`.env.example`](.env.example). The ones that matter most:

| Variable | Default | Notes |
| --- | --- | --- |
| `SECRET_KEY` | — | **Required in production**, ≥32 random chars. Missing or weak ⇒ refuses to start |
| `BOOTSTRAP_TOKEN` | empty | Required to claim a fresh database; `/setup` returns 503 without it |
| `DB_PATH` | `/app/data/data.sqlite3` | Must live on the persistent volume |
| `SESSION_COOKIE_SECURE` | `true` in production | Keep `true` behind HTTPS; set `false` for plain HTTP or login cannot work |
| `TRUST_PROXY_HEADERS` | `false` | Enable only when every request passes a trusted proxy |
| `MAX_IMPORT_SIZE_MB` | `10` | Largest JSON backup accepted |
| `AUTH_LOGIN_MAX_ATTEMPTS` | `10` | Failures per IP and route inside the window |

---

## Access control

Two modes, switched under **Settings → Access**:

- **Off** — no password. For local use, or when an identity gateway such as Cloudflare Access
  or Authelia already sits in front.
- **Access password** — one password guards every page and endpoint.

Passwords are hashed with Werkzeug (scrypt); a legacy SHA-256 hash is upgraded on the next
successful login. Changing the password or the mode requires the current password and signs out
every device. Failed logins are rate-limited per IP and route, with a second site-wide threshold
against distributed guessing.

---

## Data, backup and restore

Everything lives in the single SQLite file at `DB_PATH`; backing up means backing up `/app/data`.

**Export** — "Export JSON" on the settings page produces every prompt with its full version
history. While signed in you can also export a full backup that includes the password hash.

**Import** — pick a JSON file and confirm. It **replaces all existing data**, but a complete
snapshot of the current database is written to `dirname(DB_PATH)/backups/pre-import-*.json` first,
keeping the most recent `IMPORT_BACKUP_RETENTION` files.

Uploaded bundles are validated in full before a single row is written: ids must be safe positive
integers, timestamps must be real and not in the future, and the version graph must reference only
versions inside the same prompt with no cycles. Any failure rejects the whole file.

```bash
# Backup
docker compose exec prompt-manage sh -c 'sqlite3 /app/data/data.sqlite3 ".backup /app/data/backup.sqlite3"' \
  && docker compose cp prompt-manage:/app/data/backup.sqlite3 ./backup.sqlite3

# Restore
docker compose stop prompt-manage
docker compose cp ./backup.sqlite3 prompt-manage:/app/data/data.sqlite3
docker compose start prompt-manage
```

---

## Upgrading

```bash
# 1) back up /app/data first (above)
# 2) point PROMPT_MANAGE_IMAGE at the new tag, then
docker compose pull && docker compose up -d
docker compose logs -f prompt-manage   # "Applied migration" lines mean it is migrating
```

Coming from an older version (per-prompt passwords, cover images, favourites), migration 12 does
the following, and **never makes anything more visible than it was**:

- `auth_mode=per` → `global`: previously protected prompts still need the password, and the rest
  become protected too
- every `favorite=1` prompt becomes pinned
- any stored cover images are written to `dirname(DB_PATH)/removed-covers-*.json` before the
  column is dropped
- the `prompt_unlocks` table and the `require_password` / `copy_count` / `last_used_at` columns
  are removed

Old JSON backups still import: `favorite` folds into `pinned`, and `auth_mode=per` becomes `global`.

---

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

export APP_ENV=development
export SECRET_KEY=dev-only-key-0123456789abcdef0123456789
export DB_PATH="$PWD/data/dev.sqlite3"
export SESSION_COOKIE_SECURE=false
python app.py                     # http://127.0.0.1:3501
```

```bash
python -m pytest -q
ruff check app.py wsgi.py gunicorn.conf.py promptmanage tests
```

---

## Layout

```
app.py                 dev entry point + the stable import surface for tests/tools
wsgi.py                production entry point (gunicorn wsgi:app)
promptmanage/
  __init__.py          app factory, request lifecycle, security headers, error pages
  config.py            environment parsing and validation (fails fast at startup)
  db.py                connections and the settings table
  migrations.py        schema history
  security.py          passwords, sessions, CSRF, rate limiting, access control
  transfer.py          JSON backup export, validation and import
  utils.py             timestamps, tags, colours, version numbers, safe redirects
  i18n.py              translation table
  views/               routes: library / settings / auth / api / misc
templates/  static/    Jinja templates and assets (no build step)
```

Notes:

- Single-file SQLite with WAL; writes take `BEGIN IMMEDIATE` and a writer lock returns
  503 with `Retry-After`
- Migrations run once inside a database-wide write lock, so concurrent workers cannot race
- Sessions are recorded server-side in `auth_sessions`: changing the password invalidates every
  device, and a copied cookie stops authenticating
- Double-submit CSRF, plus CSP, `X-Frame-Options` and `Referrer-Policy` by default
- No frontend dependencies or build step: ~1.1k lines of plain JS, including a Markdown renderer
  that escapes before it parses

---

## FAQ

**Login silently fails.** Almost always plain HTTP with `SESSION_COOKIE_SECURE=true`, so the
browser drops the session cookie. Set it to `false` and restart, or serve over HTTPS.

**Forgot the password.** Stop the container and edit the database:
`sqlite3 /app/data/data.sqlite3 "UPDATE settings SET value='off' WHERE key='auth_mode';"`
then restart and set a new one in Settings.

**`/setup` returns 503.** `BOOTSTRAP_TOKEN` is not configured. Set a random value and restart;
clear it once setup is done.

**`SECRET_KEY is missing or too weak` on startup.** Production needs a ≥32-character random key:
`openssl rand -hex 32`.

---

## License

GPL-3.0-only — see [LICENSE](LICENSE).
