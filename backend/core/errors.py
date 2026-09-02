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
