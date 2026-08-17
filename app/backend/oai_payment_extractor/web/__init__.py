"""Flask and WebSocket workbench integration."""


def create_app(*args, **kwargs):
    """Load Flask lazily so the core task manager remains importable standalone."""
    from .app import create_app as factory

    return factory(*args, **kwargs)


__all__ = ["create_app"]
