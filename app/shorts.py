"""Long-only by default: the desk sells only shares it holds.

A sell of shares the desk does not hold is a short. Shorting on the paper
accounts is off unless RONATHAN_ALLOW_SHORTS is set in etc/ronathan.env
(1 / yes / true / on / enabled). The real-money account never shorts,
whatever this says (DESIGN.md §22.2).

The rule is checked where each kind of sell is made — the arbiter's thesis
(downgraded to a pass), the override and manual-trade cards, and Watchlist
price triggers — and once more in execute, which every order passes through.
"""
import os

ON = ("1", "yes", "true", "on", "enabled")

NO_SHORTS = ("the desk is long-only (RONATHAN_ALLOW_SHORTS is off): it "
             "sells only shares it holds")


def allowed(env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get("RONATHAN_ALLOW_SHORTS") or "").strip().lower() in ON
