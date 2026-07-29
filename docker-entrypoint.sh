#!/bin/sh
set -eu

# 数据目录跟随 DB_PATH（应用真正读写的库路径），并在规范化后限制在
# 容器专用的 /app/data 卷内。否则 DB_PATH=/app/data/../app.py 之类的值会让
# 下面的 chown -R 意外递归修改应用代码或系统目录。
if [ -n "${DB_PATH:-}" ]; then
  case "$DB_PATH" in
    /*) ;;
    *)
      echo "ERROR: DB_PATH must be an absolute path inside /app/data." >&2
      exit 1
      ;;
  esac
  if ! DB_PATH="$(realpath -m "$DB_PATH")"; then
    echo "ERROR: could not canonicalize DB_PATH." >&2
    exit 1
  fi
  case "$DB_PATH" in
    /app/data/*) ;;
    *)
      echo "ERROR: DB_PATH must resolve inside /app/data." >&2
      exit 1
      ;;
  esac
  DATA_DIR="$(dirname "$DB_PATH")"
else
  DB_PATH="/app/data/data.sqlite3"
  DATA_DIR="/app/data"
fi
APP_USER="${APP_USER:-appuser}"

case "$DATA_DIR" in
  /|/app|.)
    echo "ERROR: DATA_DIR must be a dedicated writable data directory, not $DATA_DIR." >&2
    exit 1
    ;;
esac

mkdir -p "$DATA_DIR" "$DATA_DIR/backups"

# 校验“最终运行用户”对数据目录是否可写；不可写就在这里明确报错并退出，
# 而不是放任 gunicorn 启动后在第一次写库时抛出晦涩的 sqlite 错误。
check_writable() {
  _u="$1"
  if [ -n "$_u" ]; then
    gosu "$_u" sh -c "[ -w \"$DATA_DIR\" ]" 2>/dev/null
  else
    [ -w "$DATA_DIR" ]
  fi
}

check_db_access() {
  if [ "$(id -u)" = "0" ]; then
    if [ -e "$DB_PATH" ]; then
      gosu "$APP_USER" sh -c 'test -r "$1" && test -w "$1"' sh "$DB_PATH" 2>/dev/null
    else
      probe="$DATA_DIR/.prompt-manage-write-test.$$"
      gosu "$APP_USER" sh -c 'umask 077; : > "$1" && rm -f "$1"' sh "$probe" 2>/dev/null
    fi
  else
    if [ -e "$DB_PATH" ]; then
      [ -r "$DB_PATH" ] && [ -w "$DB_PATH" ]
    else
      probe="$DATA_DIR/.prompt-manage-write-test.$$"
      umask 077
      : > "$probe" && rm -f "$probe"
    fi
  fi
}

# Dokploy and other PaaS platforms often mount persistent storage as root-owned.
# The image starts as root only long enough to repair ownership, then drops to
# the unprivileged app user before starting gunicorn.
if [ "$(id -u)" = "0" ]; then
  chown -R "$APP_USER:$APP_USER" "$DATA_DIR" 2>/dev/null || true
  if ! check_writable "$APP_USER"; then
    echo "ERROR: $DATA_DIR is not writable by $APP_USER even after chown. Fix the mounted volume permissions." >&2
    exit 1
  fi
  if ! check_db_access; then
    echo "ERROR: database path $DB_PATH is not readable and writable by $APP_USER." >&2
    exit 1
  fi
  exec gosu "$APP_USER" "$@"
fi

if [ ! -w "$DATA_DIR" ]; then
  echo "ERROR: $DATA_DIR is not writable by UID $(id -u). Fix the mounted volume permissions." >&2
  exit 1
fi

if ! check_db_access; then
  echo "ERROR: database path $DB_PATH is not readable and writable by UID $(id -u)." >&2
  exit 1
fi

exec "$@"
