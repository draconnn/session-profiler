"""Command-line entry point for the read-only session snapshot exporter."""

from __future__ import annotations

if __package__:
    from .parser import main
else:
    from parser import main


if __name__ == "__main__":
    raise SystemExit(main())
