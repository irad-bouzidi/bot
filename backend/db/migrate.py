"""Apply the schema and adopt any pre-database state.

    python -m backend.db.migrate                 # apply schema, import legacy state
    python -m backend.db.migrate --check         # report only, change nothing
    python -m backend.db.migrate --skip-legacy   # apply schema only

Idempotent: safe to run against a live database, and safe to run twice. Also
runs on the `db` container's first boot via docker/db/init -- Postgres ignores
that directory once a data volume exists, which is why re-runnability here is
the primary path rather than a convenience.

No MetaTrader5 import, so this works on the research machine as well as the
trading host.
"""

import argparse
import json
import os
import sys

from backend.core.errors import DatabaseUnavailable
from backend.db import pool, repository

# Where the old sizing lived. Read once, on the first migrate, and then left
# alone -- the file is not deleted, so a rollback to the previous commit still
# finds the size someone had chosen.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEGACY_SETTINGS_FILE = os.environ.get(
    "BOT_SETTINGS_FILE", os.path.join(_REPO_ROOT, "data", "settings.json"))


def _log(msg):
    print("[migrate] %s" % msg, flush=True)


def _editable_keys_and_validator():
    """Borrow SYMBOL_CONFIG's validator without importing MetaTrader5.

    bot_manager owns `_validated` and `EDITABLE_KEYS`, but importing it pulls in
    MetaTrader5 and this script has to run on the research machine too. The
    duplication is deliberate and narrow: the legacy import is a one-shot, and
    save_settings() re-validates through the API path on every later write --
    plus the CHECK constraints in schema.sql refuse a bad value at the column.
    """
    def validated(key, value):
        value = float(value)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("%s must be a finite number" % key)
        if key == "lot_size" and value <= 0:
            raise ValueError("lot_size must be positive")
        if key == "partial_fraction" and not 0.0 <= value < 1.0:
            raise ValueError("partial_fraction must be in [0, 1)")
        return value

    return ("lot_size", "partial_fraction"), validated


def import_legacy_settings(path=None, known_symbols=None):
    """Copy data/settings.json into `symbol_settings`, once.

    Only for symbols that have no row yet. A later run must not overwrite a
    size that was changed through the dashboard with a stale number from a file
    nobody has touched since -- that would be the silent-restore failure the
    settings file was written to prevent, just with the file winning instead of
    the default.

    Returns the list of symbols actually imported.
    """
    path = path or LEGACY_SETTINGS_FILE
    keys, validated = _editable_keys_and_validator()
    try:
        with open(path) as fh:
            saved = json.load(fh)
    except (IOError, OSError):
        return []
    except ValueError:
        _log("%s is not valid JSON; nothing imported" % path)
        return []
    if not isinstance(saved, dict):
        return []

    imported = []
    for symbol, values in saved.items():
        if not isinstance(values, dict):
            continue
        # Same narrowness as the old loader: a file must not be able to
        # introduce a symbol or reach a key the UI cannot edit.
        if known_symbols is not None and symbol not in known_symbols:
            _log("ignoring %s -- not a configured symbol" % symbol)
            continue
        clean = {}
        for key in keys:
            if key in values:
                try:
                    clean[key] = validated(key, values[key])
                except (TypeError, ValueError) as exc:
                    _log("ignoring %s.%s -- %s" % (symbol, key, exc))
        if "lot_size" not in clean:
            continue
        existing = repository.load_settings([symbol])
        if existing.get(symbol):
            _log("%s already has a row; leaving the database value alone" % symbol)
            continue
        repository.save_settings(
            symbol, clean["lot_size"], clean.get("partial_fraction", 0.0),
            source="legacy-file",
            notes="imported from %s" % os.path.basename(path))
        _log("imported %s lot_size=%g partial_fraction=%g"
             % (symbol, clean["lot_size"], clean.get("partial_fraction", 0.0)))
        imported.append(symbol)
    return imported


def seed_defaults(defaults):
    """Write a row for any configured symbol that has none.

    So that a fresh database is a complete one: `/settings` must never have to
    answer "no row" for a configured symbol, because the fallback for a missing
    row would be the 0.1 code default -- and the whole point of persisting
    sizing is that the default never quietly comes back.

    `defaults` is {symbol: (lot_size, partial_fraction)}.
    """
    seeded = []
    for symbol, (lot, fraction) in defaults.items():
        existing = repository.load_settings([symbol])
        if existing.get(symbol):
            continue
        repository.save_settings(symbol, lot, fraction, source="code-default",
                                 notes="seeded from SYMBOL_CONFIG")
        _log("seeded %s lot_size=%g partial_fraction=%g" % (symbol, lot, fraction))
        seeded.append(symbol)
    return seeded


def _code_defaults():
    """SYMBOL_CONFIG's shipped sizing, without importing MetaTrader5.

    The values are read out of bot_manager's source rather than imported,
    because `import backend.bot_manager` needs a terminal-capable host. Falls
    back to the documented XAUUSDm pair if the parse finds nothing, and says so.
    """
    src = os.path.join(_REPO_ROOT, "backend", "bot_manager.py")
    defaults = {}
    try:
        import ast
        with open(src) as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "SYMBOL_CONFIG" not in names:
                continue
            table = ast.literal_eval(node.value)
            for symbol, cfg in table.items():
                defaults[symbol] = (float(cfg["lot_size"]),
                                    float(cfg.get("partial_fraction", 0.0)))
    except Exception as exc:
        _log("could not read SYMBOL_CONFIG from %s (%s)" % (src, exc))
    if not defaults:
        _log("falling back to the documented XAUUSDm default 0.1 / 0.5")
        defaults = {"XAUUSDm": (0.1, 0.5)}
    return defaults


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="report connectivity and schema version, change nothing")
    parser.add_argument("--skip-legacy", action="store_true",
                        help="do not import data/settings.json")
    parser.add_argument("--settings-file", default=None,
                        help="override the legacy settings.json path")
    args = parser.parse_args(argv)

    url = pool.redact(pool.database_url())
    _log("database: %s" % url)

    try:
        if args.check:
            ok = pool.ping()
            _log("reachable: %s" % ok)
            if not ok:
                return 1
            version = repository.schema_version()
            _log("schema version: %d" % version)
            _log("all tables present: %s" % repository.tables_present())
            return 0 if version > 0 else 1

        repository.apply_schema()
        _log("schema applied (version %d)" % repository.schema_version())

        defaults = _code_defaults()
        repository.ensure_bot_rows(sorted(defaults))

        if not args.skip_legacy:
            import_legacy_settings(args.settings_file, known_symbols=set(defaults))
        seed_defaults(defaults)

        _log("done")
        return 0
    except DatabaseUnavailable as exc:
        _log("FAILED: %s" % exc)
        return 1
    finally:
        pool.close_pool()


if __name__ == "__main__":
    sys.exit(main())
