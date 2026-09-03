class BotError(Exception):
    pass


class DataUnavailable(BotError):
    """Requested bars are not in the cache and no upstream source is available.

    Always raised with the exact snapshot command to run. The failure this
    replaces was silent: missing history produced all-NaN indicator values, every
    comparison against NaN returned False, and the bot simply never traded while
    reporting itself healthy.
    """


class BrokerError(BotError):
    pass


class RiskRejected(BotError):
    pass


class ConfigRejected(BotError):
    """A settings change the bot refuses to apply, with the reason for the user.

    Raised rather than returned so the message reaches the UI verbatim: a sizing
    edit that silently does nothing (or silently does something else) is worse
    than one that is refused, because `lot_size` is the only risk control this
    bot has.
    """


class DatabaseUnavailable(BotError):
    """Postgres is configured but not reachable, or the schema is not applied.

    Raised rather than swallowed for the same reason as ConfigRejected: the
    database now holds `lot_size`, and `lot_size` is the only risk control this
    bot has. A missing row must never silently resolve to the 0.1 default in
    code, because that would restore ~$70/trade of exposure for someone who had
    deliberately lowered it -- which is the exact failure the old
    write-then-rename settings file existed to prevent. Every message carries
    the command to run.
    """
