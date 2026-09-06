"""Shared test helpers."""

import json

CSRF = "testtoken"


def set_csrf(client, token=CSRF):
    """Pin a known CSRF token into the session so POSTs can include it."""
    with client.session_transaction() as session:
        session["csrf_token"] = token
    return token


def _standalone_conn(appmod):
    """A connection that does not depend on an app/request context."""
    return appmod.connect(appmod.app.config["DB_PATH"])


def seed_password(appmod, raw, mode="global"):
    conn = _standalone_conn(appmod)
    appmod.set_setting(conn, "auth_password_hash", appmod.hash_password(raw))
    appmod.set_setting(conn, "auth_mode", mode)
    conn.commit()
    conn.close()


def seed_legacy_password(appmod, raw, mode="global"):
    import hashlib

    conn = _standalone_conn(appmod)
    appmod.set_setting(
        conn, "auth_password_hash", hashlib.sha256(raw.encode("utf-8")).hexdigest()
    )
    appmod.set_setting(conn, "auth_mode", mode)
    conn.commit()
    conn.close()


def set_setting(appmod, key, value):
    conn = _standalone_conn(appmod)
    appmod.set_setting(conn, key, value)
    conn.commit()
    conn.close()


def get_setting(appmod, key, default=None):
    conn = _standalone_conn(appmod)
    try:
        return appmod.get_setting(conn, key, default)
    finally:
        conn.close()


def create_prompt(appmod, name="P", content="C", tags=None, source=None, notes=None,
                  pinned=0, archived=False, versions=1):
    conn = _standalone_conn(appmod)
    cur = conn.cursor()
    ts = appmod.now_ts()
    cur.execute(
        "INSERT INTO prompts(name, source, notes, color, tags, pinned, archived_at, "
        "created_at, updated_at) VALUES(?,?,?,NULL,?,?,?,?,?)",
        (name, source, notes, json.dumps(tags or [], ensure_ascii=False),
         1 if pinned else 0, ts if archived else None, ts, ts),
    )
    prompt_id = cur.lastrowid
    parent = None
    for index in range(versions):
        cur.execute(
            "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) "
            "VALUES(?,?,?,?,?)",
            (prompt_id, f"1.0.{index}", content if index == versions - 1 else f"{content}-v{index}",
             ts, parent),
        )
        parent = cur.lastrowid
    cur.execute("UPDATE prompts SET current_version_id=? WHERE id=?", (parent, prompt_id))
    conn.commit()
    conn.close()
    return prompt_id


def prompt_row(appmod, prompt_id):
    conn = _standalone_conn(appmod)
    try:
        return conn.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone()
    finally:
        conn.close()


def current_content(appmod, prompt_id):
    conn = _standalone_conn(appmod)
    try:
        row = conn.execute(
            "SELECT v.content FROM prompts p JOIN versions v ON v.id = p.current_version_id "
            "WHERE p.id=?",
            (prompt_id,),
        ).fetchone()
        return row["content"] if row else None
    finally:
        conn.close()


def login(client, appmod, raw, mode="global"):
    seed_password(appmod, raw, mode)
    set_csrf(client)
    return client.post("/login", data={"password": raw, "_csrf_token": CSRF}, follow_redirects=False)


def clone_session(appmod, client):
    """A second client carrying the same session cookie (a copied login)."""
    other = appmod.app.test_client()
    other.set_cookie("session", client.get_cookie("session").value)
    return other
