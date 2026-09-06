"""A tiny route registry.

Blueprints would rename every endpoint (``library.index`` instead of ``index``)
and break the ``url_for`` calls spread across the templates. This registry keeps
the flat endpoint names while still letting routes live in separate modules.
"""

_ROUTES = []


def route(rule, **options):
    def decorator(fn):
        _ROUTES.append((rule, dict(options), fn))
        return fn
    return decorator


def apply_routes(app):
    for rule, options, fn in _ROUTES:
        options = dict(options)
        endpoint = options.pop("endpoint", fn.__name__)
        app.add_url_rule(rule, endpoint, fn, **options)
