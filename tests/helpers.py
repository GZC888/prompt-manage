"""Shared test helpers."""
import json
import os
import secrets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CSRF = "testtoken"


def set_csrf(client, token=CSRF):
    """Pin a known CSRF token into the session so POSTs can include it."""
    with client.session_transaction() as sess:
        sess["csrf_token"] = token
    return token


def seed_password(appmod, raw, mode="global"):
    conn = appmod.get_db()
    appmod.set_setting(conn, "auth_password_hash", appmod.hash_password(raw))
    appmod.set_setting(conn, "auth_mode", mode)
    conn.commit()
    conn.close()


def seed_legacy_password(appmod, raw, mode="global"):
    import hashlib
    conn = appmod.get_db()
    appmod.set_setting(conn, "auth_password_hash", hashlib.sha256(raw.encode("utf-8")).hexdigest())
    appmod.set_setting(conn, "auth_mode", mode)
    conn.commit()
    conn.close()


def create_prompt(appmod, name="P", content="C", require_password=0, tags=None, source=None, notes=None, favorite=0, archived=False):
    conn = appmod.get_db()
    cur = conn.cursor()
    ts = appmod.now_ts()
    cur.execute(
        "INSERT INTO prompts(name, source, notes, color, tags, image_data, pinned, favorite, "
        "archived_at, last_used_at, copy_count, created_at, updated_at, require_password) "
        "VALUES(?,?,?,?,?,?,0,?,?,NULL,0,?,?,?)",
        (name, source, notes, None, json.dumps(tags or [], ensure_ascii=False), None,
         1 if favorite else 0, ts if archived else None, ts, ts, require_password),
    )
    pid = cur.lastrowid
    cur.execute(
        "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) VALUES(?,?,?,?,NULL)",
        (pid, "1.0.0", content, ts),
    )
    cur.execute("UPDATE prompts SET current_version_id=? WHERE id=?", (cur.lastrowid, pid))
    conn.commit()
    conn.close()
    return pid


def unlock(client, prompt_id, appmod=None):
    if appmod is None:
        import app as appmod
    with client.session_transaction() as sess:
        sid = sess.get("sid")
        if not sid:
            sid = secrets.token_urlsafe(32)
            sess["sid"] = sid
        sess.permanent = True
        sess.pop("unlocked_prompts", None)
    conn = appmod.get_db()
    revision = appmod.get_setting(conn, "auth_revision", "1") or "1"
    conn.execute(
        "INSERT OR IGNORE INTO prompt_unlocks("
        "session_id, prompt_id, unlocked_at, auth_revision) VALUES(?,?,?,?)",
        (sid, prompt_id, appmod.now_ts(), str(revision)),
    )
    conn.commit()
    conn.close()


def login(client, appmod, raw, mode="global"):
    seed_password(appmod, raw, mode)
    set_csrf(client)
    return client.post("/login", data={"password": raw, "_csrf_token": CSRF}, follow_redirects=False)
