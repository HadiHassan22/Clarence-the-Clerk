"""Who a vote is counted against, and how many of them it takes.

Thresholds here count against the roster rather than against turnout, and that
single choice is what makes a vote able to end early. If five of eight are
needed and the fifth yes lands, nothing anyone does afterwards can change the
result, so there is no reason to keep the ballot open for another two days.

It has one consequence worth being deliberate about: somebody absent counts the
same as somebody voting no. Away is the release valve. It is meant to be
frictionless and free of judgement -- a quiet fortnight sets it on its own, and
nobody is ever expected to explain themselves.

That rule is the cooperative's, and it only works because the cooperative is a
few people who all signed up to be counted -- which is why the cooperative is
the only electorate there is: a roll somebody joined on purpose is one where
saying nothing is a position, and a whole server is not.

Every number either rule uses arrives as an argument. The house sets them; the
defaults below are only what he came with.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

AUTO_AWAY_DAYS = 14
AWAY_ROLE = "away"

# What share of the roster each tier needs. "normal" is a plain majority and
# is the default for everything, because it is the rule people already expect
# from a vote. At eight on the roster it asks for five, which is the same
# number a two-thirds rule would have produced anyway.
TIERS = {"normal": None, "fundamental": 0.75}

_path = None


def configure(data: Path):
    global _path
    _path = data / "roster.json"


def _load():
    if _path is None or not _path.exists():
        return {"seen": {}}
    try:
        return json.loads(_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"seen": {}}


def _save(state):
    if _path is None:
        return
    tmp = _path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(_path)


def touch(user_id):
    """Record that somebody is around; called whenever they speak."""
    state = _load()
    state.setdefault("seen", {})[str(user_id)] = datetime.now(timezone.utc).isoformat()
    _save(state)


def last_seen(user_id):
    raw = _load().get("seen", {}).get(str(user_id))
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def away_reason(member, away_days=AUTO_AWAY_DAYS):
    """Why somebody is out of the count, or None if they are still in it.

    Two ways in, and the difference matters to exactly one caller: `role` is a
    decision they made and already know about, `quiet` is one Eugene made on
    their behalf while they were not looking, and that one he owes them a word
    about.
    """
    if any(r.name.lower() == AWAY_ROLE for r in getattr(member, "roles", [])):
        return "role"
    seen = last_seen(member.id)
    if seen is None:
        # Nobody seen since records began counts as present. A fresh state
        # file should not quietly empty the roster and drop every threshold
        # to one; it takes a real quiet fortnight to step somebody out.
        return None
    quiet = datetime.now(timezone.utc) - seen > timedelta(days=away_days)
    return "quiet" if quiet else None


def is_away(member, away_days=AUTO_AWAY_DAYS):
    """Away by choice (an 'away' role) or by absence. Never a judgement."""
    return away_reason(member, away_days) is not None


def active(guild, belongs, exclude=(), away_days=AUTO_AWAY_DAYS):
    """Every id a vote is counted against, sorted so it is stable.

    `belongs` decides who is even a candidate, and it is the caller's to
    choose because it is not always the cooperative: a poll open to the
    whole server is counted against the whole server. It is always the same
    test the ballot itself admits people by, so the denominator can never
    hold somebody the buttons would turn away.
    """
    return sorted(
        m.id
        for m in guild.members
        if not m.bot and belongs(m) and m.id not in exclude
        and not is_away(m, away_days)
    )


def required(size, tier="normal", fundamental_share=None):
    """How many yes votes carry a roster of this size.

    Never returns more than the roster holds, so a threshold can't become
    unreachable, and never less than a majority, so a supermajority tier can
    never ask for fewer votes than an ordinary one.
    """
    if size <= 0:
        return 1
    majority = size // 2 + 1
    share = TIERS.get(tier)
    if tier == "fundamental" and fundamental_share is not None:
        share = fundamental_share
    if share is None:
        return majority
    return min(size, max(math.ceil(size * share), majority))


