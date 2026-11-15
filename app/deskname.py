"""Two names, both settings — so a second desk can be called something else
and be run by an assistant with another name (the code, its settings and
its commands keep the word "ronathan").

  RONATHAN_ASSISTANT_NAME  the LLM that runs the desk and talks to you:
                           "Ask Ronathan", "Ronathan asks you before
                           selling", "cannot reach Ronathan", "You are
                           Ronathan…"            (default Ronathan)
  RONATHAN_DESK_NAME       the desk itself: the page titles ("Ronathan —
                           Portfolio"), the menu's label, the title on
                           Telegram alerts and notifications ("Ronathan:
                           …"), "Ronathan desk bot"
                                      (default: the assistant's name)

The names are applied where words LEAVE the desk, not at each of the
hundreds of places that say "Ronathan": the pages and their data (the
dashboards' response writer), Telegram messages and desktop notifications
(app/alerts.py), and the prompts given to the models
(registry.render_prompt — so the chat introduces itself by the right name).
With both defaults nothing is touched at all.

Which name a "Ronathan" becomes is decided by what follows or precedes it:
the few places that name the DESK are patterns (DESK_PATTERNS); every other
whole-word "Ronathan" is the assistant. Never part of a longer word or an
identifier (ronathan_chat, x-ronathan-secret, /ronathan). Both names are limited
to letters, digits, spaces and hyphens so they are safe to drop into a
page, a script string or a Telegram message as they stand.
"""
import os
import re

DEFAULT = "Ronathan"
_W = r"(?<![A-Za-z0-9_])Ronathan(?![A-Za-z0-9_])"
# where "Ronathan" is the DESK, not the assistant
DESK_PATTERNS = (
    r"(?<=<title>)Ronathan(?= — )",           # every page's title
    r'(?<=aria-label=")Ronathan(?=")',        # the menu's label
    r"(?<![A-Za-z0-9_])Ronathan Trading(?![A-Za-z0-9_])",
    r"(?<![A-Za-z0-9_])Ronathan(?= desk\b)",  # "Ronathan desk bot", "Ronathan desk"
)
_DESK = re.compile("|".join(DESK_PATTERNS))
_ANY = re.compile(_W)
_VALID = re.compile(r"[A-Za-z][A-Za-z0-9 \-]{0,23}")


class NameError_(ValueError):
    pass


def _read(env, key):
    raw = ((os.environ if env is None else env).get(key) or "").strip()
    if raw and not _VALID.fullmatch(raw):
        raise NameError_(
            f"{key}={raw!r}: use 1–24 letters, digits, spaces or hyphens, "
            "starting with a letter")
    return raw


def assistant(env=None) -> str:
    """The LLM that runs the desk."""
    return _read(env, "RONATHAN_ASSISTANT_NAME") or DEFAULT


def desk(env=None) -> str:
    """The desk itself; the assistant's name unless it has its own."""
    return _read(env, "RONATHAN_DESK_NAME") or assistant(env)


def check(env=None):
    """At start: raises on a bad value, returns (desk, assistant)."""
    return desk(env), assistant(env)


def _names(env=None):
    try:
        return check(env)
    except NameError_:              # a running desk never dies on its name
        return DEFAULT, DEFAULT


def version(env=None) -> str:
    """'' with the defaults, else a short tag of both names — part of a
    page's ETag, so a rename is never served from the browser's old copy."""
    d, a = _names(env)
    return "" if (d, a) == (DEFAULT, DEFAULT) else \
        (d + "~" + a).replace(" ", "_")


def brand(text, env=None):
    """`text` as the desk says it: its own name where "Ronathan" names the
    desk, the assistant's everywhere else."""
    d, a = _names(env)
    if (d, a) == (DEFAULT, DEFAULT) or not isinstance(text, str) \
            or "Ronathan" not in text:
        return text
    return _ANY.sub(a, _DESK.sub(d, text))


def brand_bytes(data: bytes, env=None) -> bytes:
    if b"Ronathan" not in data or _names(env) == (DEFAULT, DEFAULT):
        return data
    return brand(data.decode("utf-8", "surrogateescape"), env).encode(
        "utf-8", "surrogateescape")
