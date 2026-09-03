"""Postgres persistence.

Deliberately free of any MetaTrader5 import (the MT5 invariant in CLAUDE.md),
and free of any psycopg2 import at module scope -- see `pool` for why.
"""
