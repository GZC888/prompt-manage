# Prompt Manage

Language: [简体中文](README.md) | English

<p align="center">
  <img src="logo.png" alt="Prompt Manager Logo" width="140" />
</p>

A **lightweight, self-hosted** personal prompt manager: version control, search, tags, sources, favorites, archive, images, import/export, EN/ZH UI and light/dark themes. Deliberately lightweight stack — **Python + Flask + SQLite + Jinja + vanilla JS/CSS** — no React/Vue/Next.js, no external CDN, ready for long-term hosting on a personal VPS.

> Image: `ghcr.io/gzc888/prompt-manage`

## Quick start (Docker Compose)

Choose an image first. Production deployments should use a release tag,
`sha-*` tag, or digest; `latest` is for temporary validation only.

```bash
export PROMPT_MANAGE_IMAGE=ghcr.io/gzc888/prompt-manage:sha-REPLACE_ME
# Replace sha-REPLACE_ME with the exact release/sha tag or image digest.
```

Create two different random values. `BOOTSTRAP_TOKEN` is used only to claim a
brand-new production database:

```bash
umask 077
printf 'PROMPT_MANAGE_IMAGE=%s\n' "$PROMPT_MANAGE_IMAGE" > .env
printf 'HOST_BIND=127.0.0.1\n' >> .env
printf 'SECRET_KEY=%s\n' "$(openssl rand -hex 32)" >> .env
printf 'BOOTSTRAP_TOKEN=%s\n' "$(openssl rand -hex 32)" >> .env
printf 'SESSION_COOKIE_SECURE=false\n' >> .env       # direct HTTP access
docker compose up -d
```

On a new production database, business routes stay closed until setup is
complete. A random `BOOTSTRAP_TOKEN` of at least 32 characters must be
configured first; known placeholders and short values make the application
refuse to start, while an empty value leaves `/setup` at HTTP 503. With the example
loopback binding, open
`http://127.0.0.1:3501/setup`; through a reverse proxy, use the proxy hostname.
Enter the token, set an 8+ character password, and choose an auth mode (`global`
is recommended for public hosting).
The token is compared in constant time, is never stored in SQLite, and becomes
unusable after setup. Remove `BOOTSTRAP_TOKEN` from the environment and restart.
Existing databases remain accessible during upgrades and are not forced through
setup again.

Compose binds the published port to `127.0.0.1` by default and uses
`pull_policy: missing`; run `docker compose pull` after changing a tag/digest.
Data persists in the Compose logical volume `prompt-data` (`/app/data`). Docker
normally prefixes the engine volume name with the Compose project name; inspect
the exact mounted name with
`docker inspect prompt-manage --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Name}}{{end}}{{end}}'`.
Compose allows an empty
`BOOTSTRAP_TOKEN` after setup so it can be removed from the environment; on a
brand-new production volume the application keeps `/setup` at HTTP 503 and all
business routes closed until a token is configured. Do not commit `.env` or
bootstrap credentials. For temporary direct LAN access, set `HOST_BIND=0.0.0.0`
behind a firewall and keep `TRUST_PROXY_HEADERS=false`.

## Dokploy

Create a **Compose** app from `docker-compose.yml`, set `PROMPT_MANAGE_IMAGE` to
an immutable tag/digest, and set different `SECRET_KEY` and `BOOTSTRAP_TOKEN`
values (`openssl rand -hex 32`). Behind an
HTTPS reverse proxy, set `SESSION_COOKIE_SECURE=true`. Set
`TRUST_PROXY_HEADERS=true` only when every request reaches the app through one
trusted nearest proxy that overwrites `X-Forwarded-For` and
`X-Forwarded-Proto`; never expose port `3501` directly in that configuration.
For multiple proxy hops, normalize the headers at the proxy nearest the app.
Mount persistent storage at `/app/data`. Complete `/setup`, remove
`BOOTSTRAP_TOKEN`, and redeploy.

### HTTPS, HSTS, and Cloudflare

Secure cookies and HSTS do not provide TLS. Terminate valid HTTPS at Dokploy,
Nginx, Caddy, or a Cloudflare Origin and redirect HTTP to HTTPS first.
Production enables `ENABLE_HSTS` by default, but the app sends
`Strict-Transport-Security: max-age=<HSTS_MAX_AGE>` only for requests it
recognizes as HTTPS. `HSTS_INCLUDE_SUBDOMAINS` defaults to `false`; set it to
`true` only after every affected subdomain supports HTTPS to append
`includeSubDomains`. `HSTS_MAX_AGE=0` asks browsers to clear the policy.

With Cloudflare, use **Full (strict)** TLS and disable **Rocket Loader** for this
application hostname. Rocket Loader can reorder the early layout and interaction
scripts. Do not cache login, settings, or other dynamic HTML responses.

## GHCR / CI

GitHub Actions builds and pushes on push to `main` (`:latest`, `:sha-xxxxxxx`), on `v*` tags (`:1.2.3`, `:1.2`), and via manual dispatch. Permissions are minimal (`contents: read`, `packages: write`).

```bash
set -eu
docker pull "$PROMPT_MANAGE_IMAGE"
docker rm -f prompt-manage-verify >/dev/null 2>&1 || true
docker run -d --name prompt-manage-verify -p 127.0.0.1:3501:3501 \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -e BOOTSTRAP_TOKEN="$(openssl rand -hex 32)" \
  -e SESSION_COOKIE_SECURE=false \
  "$PROMPT_MANAGE_IMAGE"
attempt=1
until curl -fsS http://127.0.0.1:3501/healthz 2>/dev/null; do
  if [ "$attempt" -ge 30 ]; then
    docker logs prompt-manage-verify || true
    docker rm -f prompt-manage-verify >/dev/null 2>&1 || true
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 1
done
docker rm -f prompt-manage-verify >/dev/null
```

`GET /healthz` returns `{"status":"ok","build_sha":"...","initialized":true|false}`. This proves that the
container and database are reachable; a new production database still requires
the `/setup` flow above.

## Environment variables

See [`.env.example`](.env.example) for the complete list.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROMPT_MANAGE_IMAGE` | none | **Required by Compose.** Use a release/`sha-*` tag or digest in production. |
| `HOST_BIND` | `127.0.0.1` | Host address for the published port; use `0.0.0.0` only behind a firewall/private network. |
| `HOST_PORT` | `3501` | Host-side published port. |
| `APP_ENV` | `production` | `production`, `development`, or `testing`. Compose pins `production`; other values are only for source runs or a custom override. |
| `APP_PORT` | `3501` | Application listen port. |
| `DB_PATH` | `/app/data/data.sqlite3` | SQLite path. In Docker it must be an absolute path inside `/app/data`; persist that whole directory. |
| `SECRET_KEY` | none | **Required in production.** Generate with `openssl rand -hex 32`. |
| `BOOTSTRAP_TOKEN` | none | **Required for first setup** of a new production DB; an empty value makes `/setup` return 503. Remove and restart after setup. |
| `SESSION_COOKIE_SECURE` | production `true` | Set `true` only when users reach the app over HTTPS. |
| `SESSION_COOKIE_SAMESITE` | `Lax` | `Lax`, `Strict`, or `None`. |
| `TRUST_PROXY_HEADERS` | `false` | Trust one nearest proxy's client-IP and scheme headers. Never combine with direct port access. |
| `PERMANENT_SESSION_DAYS` | `3650` | Owner login lifetime. |
| `AUTH_LOGIN_MAX_ATTEMPTS` | `10` | Failed attempts allowed per IP and route in one window. |
| `AUTH_LOGIN_WINDOW_SECONDS` | `900` | Per-IP failure-count window in seconds. |
| `AUTH_LOCK_SECONDS` | `900` | Lock duration after the per-IP limit is reached. |
| `GLOBAL_LOGIN_MAX_ATTEMPTS` | `1000` | Site-wide failed-attempt ceiling against distributed guessing. |
| `GLOBAL_LOGIN_WINDOW_SECONDS` | `3600` | Site-wide failure-count window in seconds. |
| `MAX_IMPORT_SIZE_MB` | `10` | Maximum import file size. |
| `MAX_IMAGE_SIZE_MB` | `5` | Maximum size of one cover image. |
| `IMPORT_BACKUP_RETENTION` | `20` | Maximum retained pre-import JSON snapshots. |
| `ENABLE_SECURITY_HEADERS` | `true` | Emit CSP and other security headers. |
| `ENABLE_HSTS` | production `true` | Emit HSTS only on requests recognized as HTTPS; does not enable TLS. |
| `HSTS_MAX_AGE` | `31536000` | HSTS lifetime in seconds. |
| `HSTS_INCLUDE_SUBDOMAINS` | `false` | Append `includeSubDomains`; enable only when every affected subdomain supports HTTPS. |
| `GUNICORN_WORKERS` | `2` | Gunicorn worker processes; do not raise blindly with SQLite writes. |
| `GUNICORN_THREADS` | `4` | Threads per worker. |
| `GUNICORN_TIMEOUT` | `60` | Request timeout in seconds. |
| `GUNICORN_GRACEFUL_TIMEOUT` | `30` | Graceful shutdown timeout in seconds. |
| `GUNICORN_LOG_LEVEL` | `info` | Gunicorn log level. |
| `BUILD_SHA` | `dev` (build time) | Build identifier exposed by `/healthz`; CI injects it into release images, so do not override it at runtime. |
| `FLASK_DEBUG` | `false` | Used only by local `python app.py`; never enable in production. |

Never commit real `SECRET_KEY` or `BOOTSTRAP_TOKEN` values, or put the bootstrap
token in a URL or support ticket. An empty bootstrap token is not a supported
passwordless first-run mode.

## Auth modes

- `off` — no password, fully open (local/intranet only).
- `global` — password gates the whole site, log in once. **Recommended for public deployments** (`off` has no protection; `per` only protects flagged prompts).
- `per` — site is browsable, but prompts flagged "require password" are individually locked; their content/tags/source/notes never appear in lists, search, the tags API, or default exports.

Passwords must be **at least 8 chars with no maximum length** (long passphrases encouraged). Legacy 4–8 digit passwords still log in and are transparently upgraded to a Werkzeug hash. `/settings` and `/export` require login once a password is set.
In `per` mode, the prompt unlock password is the same owner credential used for
full administration. Anyone who knows it can modify or delete every prompt; do
not distribute it as a read-only sharing password.

## Data, backup, restore, and upgrade

SQLite uses WAL, so a running database may consist of `data.sqlite3` plus
`data.sqlite3-wal` and `data.sqlite3-shm`. Persist the whole `/app/data`
directory. **Do not copy only `data.sqlite3` while the app is running**: committed
transactions may still exist only in the WAL.

JSON/CSV is a portable **logical export** of prompt content, versions, and
selected non-auth settings. Normal exports do not contain credentials. A logged-in
owner can explicitly request `include_locked=1&include_auth=1` for a full logical
restore export; that file includes the auth mode and **password hash**, so treat it
as a secret: encrypt it and restrict access. It does not contain every SQLite
state such as rate-limit rows, unlock sessions, or migration tables. In `per`
mode, the normal export may also omit protected prompts. Pre-import JSON files in
`dirname(DB_PATH)/backups/pre-import-*.json` (by default,
`/app/data/backups/pre-import-*.json`) include the prompt data and recoverable
settings, but are same-volume rollback points, not off-site disaster-recovery
snapshots. A normal import restores general settings but deliberately does not
replace the current auth credentials unless an explicit high-risk auth-restore
operation is requested.

For a consistent online physical snapshot, use SQLite's Online Backup API and
then copy the result off the container:

```bash
set -eu
umask 077
backup="prompt-data-$(date +%Y%m%d-%H%M%S).sqlite3"
backup_path="$(docker exec -e BACKUP_NAME="$backup" prompt-manage \
  python -c 'import os; print(os.path.join(os.path.dirname(os.environ["DB_PATH"]), "backups", os.environ["BACKUP_NAME"]))')"
docker exec -e BACKUP_FILE="$backup_path" prompt-manage \
  python -c 'import os,sqlite3; os.umask(0o077); src=sqlite3.connect(os.environ["DB_PATH"]); dst=sqlite3.connect(os.environ["BACKUP_FILE"]); src.backup(dst); dst.close(); src.close()'
if docker cp "prompt-manage:$backup_path" "./$backup" && chmod 600 "./$backup"; then
  docker exec prompt-manage rm -f -- "$backup_path" || \
    echo "Snapshot copied, but temporary volume file cleanup failed: $backup_path" >&2
else
  echo "Copy failed; the snapshot remains at $backup_path. Verify a retry before deleting it manually." >&2
  exit 1
fi
```

The volume-side temporary snapshot is deleted only after both `docker cp` and the
local permission change succeed. If copying or cleanup fails, retry and verify the
local file first, then run `docker exec prompt-manage rm -f -- <path>` using the
reported path; do not delete the only usable copy first.

For an offline volume archive, stop the service and copy all of `/app/data`,
including any WAL/SHM files, before starting it again:

```bash
set -eu
umask 077
install -d -m 700 ./backups
docker compose stop prompt-manage
trap 'docker compose start prompt-manage' EXIT
docker compose run --rm --no-deps -v "$PWD/backups:/backup" --entrypoint sh prompt-manage \
  -c 'umask 077; tar -czf "/backup/prompt-data-$(date +%Y%m%d-%H%M%S).tar.gz" -C /app/data .'
docker compose start prompt-manage
trap - EXIT
```

Physical snapshots and volume archives contain password hashes and all protected
content. Encrypt them, restrict access, keep them outside web-served paths, and
run `PRAGMA integrity_check` before relying on a restore.

Upgrade with `docker compose pull && docker compose up -d`. Startup migrations
are recorded in `schema_migrations` and abort startup on failure. A migration may
transactionally create a replacement table, copy data, drop the old table, and
rename the replacement to add constraints. Do not rely on a “no drop/rename”
guarantee. Take a restorable physical backup first. For repeatable releases and
rollback, pin a release tag, `sha-*` tag, or image digest instead of `latest`.

### Restore drill

Before each upgrade and at least monthly, run the following commands in the same
shell. First create a uniquely named empty volume, restore one of the two physical
backup formats below, and then start the test container. File variables must
contain the complete relative filename, including its extension.

```bash
set -eu
restore_suffix="$(date +%Y%m%d%H%M%S)-$$"
restore_volume="prompt-restore-test-$restore_suffix"
restore_container="prompt-restore-test-$restore_suffix"
docker volume create "$restore_volume"
```

Restore an online `.sqlite3` snapshot from the current directory:

```bash
snapshot="prompt-data-YYYYMMDD-HHMMSS.sqlite3"
docker run --rm -v "$restore_volume:/data" -v "$PWD:/restore:ro" alpine:3.20 \
  sh -c 'cp "/restore/$1" /data/data.sqlite3' sh "$snapshot"
```

Or restore an offline volume archive from `./backups`. Extract it into an empty
volume; never merge it over stale volume files:

```bash
archive="backups/prompt-data-YYYYMMDD-HHMMSS.tar.gz"
docker run --rm -v "$restore_volume:/data" -v "$PWD:/restore:ro" alpine:3.20 \
  sh -c 'tar -xzf "/restore/$1" -C /data' sh "$archive"
```

Start the same image bound only to loopback. Keep the default path below for the
`.sqlite3` snapshot; when restoring an archive from a deployment with a custom
`DB_PATH`, set `restore_db_path` to that original value:

```bash
restore_db_path=/app/data/data.sqlite3
docker run -d --name "$restore_container" -p 127.0.0.1:3502:3501 \
  -e SECRET_KEY="$(openssl rand -hex 32)" -e SESSION_COOKIE_SECURE=false \
  -e DB_PATH="$restore_db_path" \
  -v "$restore_volume:/app/data" "$PROMPT_MANAGE_IMAGE"
attempt=1
until curl -fsS http://127.0.0.1:3502/healthz 2>/dev/null; do
  if [ "$attempt" -ge 30 ]; then
    docker logs "$restore_container" || true
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 1
done
docker exec "$restore_container" python -c 'import os,sqlite3,sys; c=sqlite3.connect(os.environ["DB_PATH"]); r=c.execute("PRAGMA integrity_check").fetchall(); print(r); c.close(); sys.exit(0 if r == [("ok",)] else 1)'
```

Confirm the health response reports `initialized=true` and the integrity command
prints only `[('ok',)]`, then actually test login, representative prompts, version
history, and export. Do not test by overwriting production. For a real `.sqlite3`
restore, stop all access first, move the old database and its `-wal`/`-shm` files
aside, place the snapshot at `DB_PATH`, and restart. To restore an offline archive,
extract it into a new empty volume, mount that volume, and retain the original
`DB_PATH`; do not merge it over stale files. In either case, check migration logs,
health, auth mode, HTTPS cookies, and data. Keep at least one verified copy on a
separate host or object store.

After the checks, remove the temporary resources:

```bash
docker rm -f "$restore_container"
docker volume rm "$restore_volume"
```

### Deployment acceptance

1. `docker compose ps` reports `healthy`; logs contain no secret, volume,
   bootstrap, or migration error.
2. `curl -fsS https://<host>/healthz` reports `status=ok`, the expected
   `build_sha`, and the correct `initialized` state rather than assuming a tag
   changed.
3. After `/setup`, remove `BOOTSTRAP_TOKEN`, restart, and confirm setup cannot be
   repeated. Test login, create/export/delete a disposable prompt.
4. Check CSP and other headers with `curl -sSI https://<host>/`; when enabled,
   verify HSTS, HTTP-to-HTTPS redirect, and Secure-cookie login persistence.
5. With proxy trust enabled, verify port `3501` is unreachable directly and the
   client IP in rate-limit logs matches the actual proxy topology.
6. Complete and record an isolated restore drill. A green health endpoint alone
   does not prove that content, authentication, and backups are recoverable.

## Local development & tests

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export SECRET_KEY=dev-only APP_ENV=development DB_PATH=./data/data.sqlite3 FLASK_DEBUG=true
python app.py            # http://127.0.0.1:3501  (production uses: gunicorn -c gunicorn.conf.py wsgi:app)

pytest -q                # uses a throwaway SQLite db, never touches real data
```

License: see [LICENSE](LICENSE).
