"""Run once with the migration owner; the serving runtime does not execute DDL."""

import os

from bravo_cutover.store import Store


def main():
    store = Store(os.environ["EXECUTOR_MIGRATION_DB"])
    try:
        store.migrate()
    finally:
        store.close()


if __name__ == "__main__":
    main()
