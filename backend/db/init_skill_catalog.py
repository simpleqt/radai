"""CLI entrypoint for importing built-in Skills into the managed catalog."""

from __future__ import annotations

import argparse
import asyncio

from backend.application.skills.catalog_importer import import_catalog


def main() -> None:
    parser = argparse.ArgumentParser(description="Import built-in Skills into MySQL")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    asyncio.run(import_catalog(force=args.force))


if __name__ == "__main__":
    main()
