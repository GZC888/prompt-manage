"""Route modules. Importing them fills the registry; ``register`` applies it."""

from ..routing import apply_routes
from . import api, auth, library, misc, settings  # noqa: F401  (import registers routes)


def register(app):
    apply_routes(app)
