"""What Eugene says without being asked.

Everything else he does waits to be started by somebody: a button is pressed,
a message mentions him, a vote runs out of clock. This is the other half, and
it is the half that makes him worth having -- the small number of things he
says first, because otherwise somebody has to remember to ask.

Three of them, and the list is meant to stay short:

- a vote halfway through its window, with a quiet word to whoever has not
  voted, because a threshold counted against the roster makes silence read as
  a no and nobody should lose a vote by forgetting
- somebody the roster has stopped counting after a fortnight of quiet, told
  rather than dropped without a word -- the rules of procedure promise this
  in as many words
- decisions that passed and still want human hands, kept as a standing list
  rather than a closing report that scrolls away

Two rules hold it together.

**Nothing is ever said twice.** Every utterance is written into a ledger, and
one that could not be delivered is written down too: a nudge that failed
because somebody's direct messages are shut is not a nudge to retry every
quarter of an hour forever.

**Nothing here costs a token.** The model is never consulted; every line is
written by hand in clerk.py from what these functions return. Being proactive
is therefore free, and cannot quietly spend a server's monthly budget while
nobody is watching.

Deliberately free of Discord: this module decides what is due, and clerk.py
does the talking. That split is what makes any of it testable.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import settings

log = logging.getLogger("duties")

# How far into a vote's own window a nudge goes out. Half is late enough that
# the keen have already voted, and early enough to still be able to matter.
NUDGE_AT = 0.5

# A vote this close to its own end is left alone: it is about to close and
# report itself, and two notices minutes apart is how a helpful bot becomes
# an irritating one.
NUDGE_DEADZONE = timedelta(hours=1)

# How often the standing list of what still wants doing is pointed at out
# loud. The list itself is kept current continuously; this is only the tap on
# the shoulder.
CHASE_EVERY = timedelta(days=7)

# Ledger entries older than this are forgotten. A proposal closed three months
# ago is never going to be nudged again whatever the ledger remembers.
LEDGER_DAYS = 90

# Per server. It was one file for the whole daemon, which for a bot that
# keeps more than one house means the ledger of what has already been said
# is shared: a nudge sent in one server marks the duty done in all of them,
# and the second house is never told anything.
_root = None


def configure(data: Path):
    global _root
    _root = Path(data)


def _path(guild_id):
    if _root is None:
        return None
    return settings.state_file(guild_id, "duties.json", legacy_root=_root)


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _load(guild_id):
    path = _path(guild_id)
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # An unreadable ledger means a few things get said a second time.
        # That is a far better failure than a crashed duty loop.
        log.warning("duty ledger unreadable; starting it over")
        return {}


def _save(guild_id, state):
    path = _path(guild_id)
    if path is None:
        return
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


# ---------- the ledger of what has already been said ----------

def opening_pass(guild_id):
    """Whether this is the first round against a fresh ledger.

    It matters because none of this is retroactive news. A server that has
    been running for months has people who went quiet in April and votes that
    passed their halfway mark yesterday, and the day Eugene learns to notice
    such things is not the day to tell everybody about all of them at once.
    The opening pass writes down where things stand and says nothing.
    """
    return not _load(guild_id).get("started_at")


def mark_started(guild_id, now=None):
    state = _load(guild_id)
    state["started_at"] = _now(now).isoformat()
    _save(guild_id, state)


def said(guild_id, key):
    return key in _load(guild_id).get("said", {})


def mark_said(guild_id, key, now=None):
    """Record an utterance. Called whether or not it arrived: an undeliverable
    notice is not one to keep trying."""
    state = _load(guild_id)
    entries = state.setdefault("said", {})
    entries[key] = _now(now).isoformat()
    cutoff = _now(now) - timedelta(days=LEDGER_DAYS)
    for old, stamp in list(entries.items()):
        try:
            if datetime.fromisoformat(stamp) < cutoff:
                del entries[old]
        except ValueError:
            del entries[old]
    _save(guild_id, state)


# ---------- being left alone ----------

def muted(guild_id, user_id):
    return str(user_id) in _load(guild_id).get("muted", [])


def set_muted(guild_id, user_id, on=True):
    """Anything Eugene starts has to come with a way to stop it, or the next
    person to be nudged at a bad moment is right to resent him for it."""
    state = _load(guild_id)
    names = set(state.get("muted", []))
    names.add(str(user_id)) if on else names.discard(str(user_id))
    state["muted"] = sorted(names)
    _save(guild_id, state)
    return on


# ---------- 1. votes halfway through, and who has not voted ----------

def nudge_key(bill, user_id):
    # The round is in the key so a runoff, which is a fresh vote wearing the
    # same number, gets its own nudge rather than inheriting a spent one.
    return f"nudge:{bill['no']}:r{bill.get('round', 1)}:{user_id}"


def _window(bill):
    """When this round of a vote opened and when it ends, or None if it has
    no usable clock. A runoff resets the clock but keeps the filing date, so
    the round's own start is preferred where it exists."""
    try:
        opened = datetime.fromisoformat(
            bill.get("round_opened_at") or bill["submitted_at"]
        )
        ends = datetime.fromisoformat(bill["ends_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return (opened, ends) if ends > opened else None


def nudges_due(guild_id, bills, roll_for, now=None):
    """Every (proposal, user id) pair owed a nudge right now.

    `roll_for(bill)` gives the ids a vote is counted against, which is the
    clerk's business rather than this module's -- it depends on the roster,
    on who the vote is about, and on Discord.
    """
    now = _now(now)
    due = []
    for bill in bills:
        if bill.get("status") != "on_floor":
            continue
        window = _window(bill)
        if window is None:
            continue
        opened, ends = window
        if now >= ends - NUDGE_DEADZONE:
            continue
        elapsed = (now - opened).total_seconds() / (ends - opened).total_seconds()
        if elapsed < NUDGE_AT:
            continue
        ballots = bill.get("ballots", {})
        for user_id in roll_for(bill):
            if str(user_id) in ballots or muted(guild_id, user_id):
                continue
            if said(guild_id, nudge_key(bill, user_id)):
                continue
            due.append((bill, user_id))
    return due


# ---------- 2. the roster quietly letting somebody go ----------

def away_changes(guild_id, members, reason_for):
    """Who has just gone quiet, who has just come back, and the set to write
    down once the telling is done.

    Only the automatic kind is reported. Somebody who gave themselves the Away
    role does not need to be told what they just did.
    """
    known = set(_load(guild_id).get("quiet", []))
    quiet_now = {str(m.id) for m in members if reason_for(m) == "quiet"}
    gone = [m for m in members if str(m.id) in quiet_now - known]
    # Back means back in the count -- somebody who went from quiet to holding
    # the Away role has not come back, they have only changed how they left.
    back = [
        m for m in members
        if str(m.id) in known - quiet_now and reason_for(m) is None
    ]
    return gone, back, quiet_now


def record_quiet(guild_id, quiet_now):
    """Written after the telling, never before: a crash between the two should
    cost a repeated notice, not a silent drop."""
    state = _load(guild_id)
    state["quiet"] = sorted(quiet_now)
    _save(guild_id, state)


# ---------- 3. decisions that passed and have not happened ----------

def outstanding(bills, report_for):
    """Passed decisions that still want human hands.

    `report_for(bill)` is clerk.py's own closing report, which already works
    out what Eugene did and what he could not. Reusing it means the standing
    list and the report posted at close can never come to disagree.
    """
    items = []
    for bill in bills:
        if bill.get("status") != "passed" or bill.get("carried_out"):
            continue
        report = report_for(bill)
        if not report.get("outstanding"):
            continue
        items.append(
            {
                "no": bill["no"],
                "title": bill.get("title", ""),
                "act": bill.get("act"),
                "closed_at": bill.get("closed_at"),
                "wants": list(report["outstanding"]),
            }
        )
    return items


def chase_due(guild_id, now=None):
    """Whether the standing list is due to be pointed at out loud again."""
    last = _load(guild_id).get("chased_at")
    if not last:
        return True
    try:
        return _now(now) - datetime.fromisoformat(last) >= CHASE_EVERY
    except ValueError:
        return True


def mark_chased(guild_id, now=None):
    state = _load(guild_id)
    state["chased_at"] = _now(now).isoformat()
    _save(guild_id, state)
