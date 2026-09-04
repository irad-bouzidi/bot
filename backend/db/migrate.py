"""Apply the schema and adopt any pre-database state.

    python -m backend.db.migrate                 # apply schema, import legacy state
    python -m backend.db.migrate --check         # report only, change nothing
    python -m backend.db.migrate --skip-legacy   # apply schema only

Idempotent: safe to run against a live database, and safe to run twice. Also
runs on the `db` container's first boot via the schema.sql mounted into
/docker-entrypoint-initdb.d by docker-compose.yml -- Postgres ignores
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

    bot_manager owns `_validated`, but importing it pulls in MetaTrader5 and
    this script has to run on the research machine too. The duplicated *logic*
    is deliberate and narrow: the legacy import is a one-shot, and
    save_settings() re-validates through the API path on every later write --
    plus the CHECK constraints in schema.sql refuse a bad value at the column.
    The key *list* is imported rather than copied, so a new editable key cannot
    be added and then silently skipped by this importer.
    """
    from backend.core.symbols import BOOL_KEYS, EDITABLE_KEYS

    def validated(key, value):
        if key in BOOL_KEYS:
            # Strict for the same reason _validated() is: bool("false") is True,
            # so a string in a hand-written settings.json would turn the rule on
            # while every display of it said off. isinstance(True, int) is also
            # True, so bool has to be tested first.
            if isinstance(value, bool):
                return value
            if isinstance(value, int) and value in (0, 1):
                return bool(value)
            raise ValueError("%s must be true or false" % key)
        if isinstance(value, bool):
            # bool is a subclass of int, so float(True) is 1.0 -- a boolean
            # under `lot_size` would import as 1.0 lots, ten times the shipped
            # size, and pass every range check below.
            raise ValueError("%s must be a number, not true/false" % key)
        value = float(value)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("%s must be a finite number" % key)
        if key == "lot_size" and value <= 0:
            raise ValueError("lot_size must be positive")
        if key == "partial_fraction" and not 0.0 <= value < 1.0:
            raise ValueError("partial_fraction must be in [0, 1)")
        return value

    return EDITABLE_KEYS, validated


def import_legacy_settings(path=None, known_symbols=None, defaults=None):
    """Copy data/settings.json into `symbol_settings`, once.

    Only for symbols that have no row yet. A later run must not overwrite a
    size that was changed through the dashboard with a stale number from a file
    nobody has touched since -- that would be the silent-restore failure the
    settings file was written to prevent, just with the file winning instead of
    the default.

    `defaults` is {symbol: {key: value}} from SYMBOL_CONFIG, used for keys the
    file does not carry. It matters for `exit_at_mean`: every settings.json in
    existence predates that key, so its absence means "not recorded", NOT
    "off" -- and reading it as off would be the file quietly deciding a rule the
    file never knew about.

    Returns the list of symbols actually imported.
    """
    path = path or LEGACY_SETTINGS_FILE
    keys, validated = _editable_keys_and_validator()
    defaults = defaults or {}
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
        fallback = defaults.get(symbol, {})
        fraction = clean.get("partial_fraction", fallback.get("partial_fraction", 0.0))
        at_mean = clean.get("exit_at_mean", fallback.get("exit_at_mean", False))
        repository.save_settings(
            symbol, clean["lot_size"], fraction, at_mean,
            source="legacy-file",
            notes="imported from %s" % os.path.basename(path))
        _log("imported %s lot_size=%g partial_fraction=%g exit_at_mean=%s"
             % (symbol, clean["lot_size"], fraction, at_mean))
        imported.append(symbol)
    return imported


def seed_defaults(defaults):
    """Write a row for any configured symbol that has none.

    So that a fresh database is a complete one: `/settings` must never have to
    answer "no row" for a configured symbol, because the fallback for a missing
    row would be the 0.1 code default -- and the whole point of persisting
    sizing is that the default never quietly comes back.

    `defaults` is {symbol: {key: value}} over EDITABLE_KEYS -- a dict rather
    than a tuple so that a key added to that list does not silently shift the
    meaning of a positional element here.
    """
    seeded = []
    for symbol in sorted(defaults):
        values = defaults[symbol]
        existing = repository.load_settings([symbol])
        if existing.get(symbol):
            continue
        repository.save_settings(
            symbol, values["lot_size"], values["partial_fraction"],
            values["exit_at_mean"], source="code-default",
            notes="seeded from SYMBOL_CONFIG")
        _log("seeded %s lot_size=%g partial_fraction=%g exit_at_mean=%s"
             % (symbol, values["lot_size"], values["partial_fraction"],
                values["exit_at_mean"]))
        seeded.append(symbol)
    return seeded


def _code_defaults():
    """SYMBOL_CONFIG's shipped EDITABLE_KEYS values, without importing MetaTrader5.

    This used to `ast`-parse the dict back out of bot_manager's source, because
    `import backend.bot_manager` needs a terminal-capable host. SYMBOL_CONFIG now
    lives in `backend.core.symbols`, which imports nothing but the standard
    library, so it can simply be imported -- and a symbol added to it can no
    longer be silently skipped here by a parse that stopped matching the source
    it was reading.
    """
    from backend.core.symbols import SYMBOL_CONFIG

    return {symbol: {"lot_size": float(cfg["lot_size"]),
                     "partial_fraction": float(cfg.get("partial_fraction", 0.0)),
                     "exit_at_mean": bool(cfg.get("exit_at_mean", False))}
            for symbol, cfg in SYMBOL_CONFIG.items()}


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
            import_legacy_settings(args.settings_file, known_symbols=set(defaults),
                                   defaults=defaults)
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
