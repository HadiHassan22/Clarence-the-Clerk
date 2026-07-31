"""Who a vote is counted against, and how many of them it takes.

Thresholds here count against the roster rather than against turnout, and that
single choice is what makes a vote able to end early. If five of eight are
needed and the fifth yes lands, nothing anyone does afterwards can change the
result, so there is no reason to keep the ballot open for another two days.

It has one consequence worth being deliberate about: somebody absent counts the
same as somebody voting no. Away is the release valve. It is meant to be
frictionless and free of judgement -- a quiet fortnight sets it on its own, and
nobody is ever expected to explain themselves.
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


def away_reason(member):
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
    quiet = datetime.now(timezone.utc) - seen > timedelta(days=AUTO_AWAY_DAYS)
    return "quiet" if quiet else None


def is_away(member):
    """Away by choice (an 'away' role) or by absence. Never a judgement."""
    return away_reason(member) is not None


def active(guild, in_cooperative, exclude=()):
    """Every id a vote is counted against, sorted so it is stable."""
    return sorted(
        m.id
        for m in guild.members
        if not m.bot and in_cooperative(m) and m.id not in exclude and not is_away(m)
    )


def required(size, tier="normal"):
    """How many yes votes carry a roster of this size.

    Never returns more than the roster holds, so a threshold can't become
    unreachable, and never less than a majority, so a supermajority tier can
    never ask for fewer votes than an ordinary one.
    """
    if size <= 0:
        return 1
    majority = size // 2 + 1
    share = TIERS.get(tier)
    if share is None:
        return majority
    return min(size, max(math.ceil(size * share), majority))
