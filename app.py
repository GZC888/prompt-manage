"""Development entry point and the stable import surface for tests/tools.

The application itself lives in the :mod:`promptmanage` package; this module
re-exports the pieces that tests, the WSGI entry point and small scripts use so
those call sites do not need to know the internal layout.

Production runs ``wsgi:app`` under gunicorn. ``python app.py`` is for local
development only and never enables the debugger unless ``FLASK_DEBUG=true``.
"""

from promptmanage import PUBLIC_ENDPOINTS, app, db_busy_response  # noqa: F401
from promptmanage.config import env_bool
from promptmanage.db import (  # noqa: F401
    close_db, columns, connect, db_path, get_db, get_setting, is_locked_error, set_setting,
)
from promptmanage.i18n import LANG_DEFAULT, SUPPORTED_LANGS, translate  # noqa: F401
from promptmanage.migrations import MIGRATIONS, run_migrations  # noqa: F401
from promptmanage.security import (  # noqa: F401
    auth_configured, auth_mode, bump_auth_revision, can_manage, check_and_migrate_password,
    get_csrf_token, hash_password, is_authenticated, looks_legacy_sha256, rate_limit_status,
    record_attempt, reset_session, valid_csrf, verify_password,
)
from promptmanage.transfer import SCHEMA_VERSION, collect_export  # noqa: F401
from promptmanage.utils import (  # noqa: F401
    bump_version, now_ts, parse_int_or_none, parse_tags, sanitize_color, tags_from_row,
)
from promptmanage.views.library import prune_versions, word_diff_html  # noqa: F401


def run():
    app.run(host="0.0.0.0", port=app.config["APP_PORT"], debug=env_bool("FLASK_DEBUG", False))


if __name__ == "__main__":
    run()
