"""Allow ``python -m paperlens`` without triggering work when imported."""

from .cli import app

if __name__ == "__main__":
    app()
