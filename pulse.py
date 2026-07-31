"""The heartbeat: when Eugene is worth waking, and what he may do awake.

`duties.py` is the other proactive half and it is free -- every line it
produces is written by hand and the model is never consulted. That
guarantee is worth keeping, so this is a separate module rather than three
more functions in there: this one *does* spend, and everything here exists
to make sure it spends rarely and visibly.

The shape is a gate in front of a thought. A timer wakes the loop; the loop
works out, in plain Python and for nothing, whether anything has actually
happened since last time. Almost always nothing has, and the answer costs a
server precisely zero. Only when something has changed does a single model
call happen, and only then under a daily cap and the month's budget.

What counts as something happening is the whole design, and it is
deliberately narrow:

- people have talked since he last looked, enough of them to be a
  conversation rather than one person saying "lol"
- a vote is close to running out with people still to vote
- the standing list of decisions nobody has carried out has gone stale
- the floor has been empty for a while in a server that is otherwise busy

Three more rules hold it:

**He never says the same thing twice.** Every remark and every draft is
filed under a topic, and a topic he has raised is one he leaves alone for a
fortnight, whatever happens. A bot that raises the same idea every Tuesday
is one people mute.

**A draft is an offer, never a filing.** He does not author proposals. What
he can do is write one out and put it in front of the cooperative with a
button on it; whoever presses the button is the author, and if nobody
presses it, nothing was proposed. This is the difference between an
officeholder who notices things and one who has an agenda, and it is worth
more than the convenience of letting him file.

**Silence is the default answer.** The thought he is asked to have ends in
"say nothing" far more often than not, and the prompt says so.

Discord-free and token-free, like `duties.py`: this module decides, and
clerk.py does the talking.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("pulse")

# How often the loop wakes to look. Cheap: almost every one of these ends in
# the gate saying no, at no cost.
EVERY = timedelta(minutes=20)

# The ceiling, whatever the gate thinks. A bug in a gate condition should
# cost a server a few cents and an eyebrow, never a month's budget in a
# night, so this is checked before anything else and is not overridable by
# anything the model returns.
MAX_PER_DAY = 12

# What he must not spend the month's budget on. Below this share left, the
# heartbeat stops thinking entirely -- being answered when you speak to him
# matters more than being told something you did not ask about.
BUDGET_FLOOR = 0.25

# Enough said, by enough people, to be worth reading. One person posting
# four times is not a conversation.
MIN_MESSAGES = 8
MIN_SPEAKERS = 2

# A vote this close to its end, with people yet to vote, is worth a look.
CLOSING_SOON = timedelta(hours=6)

# How long a topic he has raised stays raised.
TOPIC_QUIET = timedelta(days=14)

# A floor this empty, in a house that is otherwise talking, is worth one
# remark and not more.
FLOOR_IDLE = timedelta(days=10)

LEDGER_DAYS = 60

_path = None


def configure(data: Path):
    global _path
    root = Path(data)
    root.mkdir(parents=True, exist_ok=True)
    _path = root / "pulse.json"


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _load():
    if _path is None or not _path.exists():
        return {}
    try:
        return json.loads(_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(state):
    if _path is None:
        return
    tmp = _path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(_path)


def _parse(raw):
    try:
        return datetime.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None


# ---------- the timer ----------

def last_pulse(now=None):
    return _parse(_load().get("last_pulse")) or _now(now) - EVERY * 2


def due(now=None):
    """Whether enough time has passed to bother looking. Looking is free;
    this only stops the loop running on top of itself."""
    return _now(now) - last_pulse(now) >= EVERY


def spent_today(now=None):
    state = _load()
    day = _now(now).strftime("%Y-%m-%d")
    return int(state.get("per_day", {}).get(day, 0))


def under_cap(now=None):
    return spent_today(now) < MAX_PER_DAY


def within_budget(spent_usd, budget_usd):
    """Whether there is enough of the month left to think unprompted.

    The floor is reserved for being spoken to. A heartbeat that eats the
    budget and leaves him mute when somebody actually asks him something
    has the priorities exactly backwards.
    """
    if not budget_usd or budget_usd <= 0:
        return False
    return (budget_usd - spent_usd) / budget_usd > BUDGET_FLOOR


def record_thought(now=None):
    """Count one model call against the day. Called whether or not he
    ended up saying anything: the thinking is what costs."""
    state = _load()
    stamp = _now(now)
    day = stamp.strftime("%Y-%m-%d")
    per_day = state.get("per_day", {})
    per_day[day] = int(per_day.get(day, 0)) + 1
    cutoff = (stamp - timedelta(days=7)).strftime("%Y-%m-%d")
    state["per_day"] = {d: n for d, n in per_day.items() if d >= cutoff}
    state["last_pulse"] = stamp.isoformat()
    _save(state)


def record_look(now=None):
    """Count one free look that did not become a thought."""
    state = _load()
    state["last_pulse"] = _now(now).isoformat()
    _save(state)


# ---------- the gate ----------

def gate(material, now=None):
    """Why this pulse is worth a thought, or None to go back to sleep.

    Every test here is arithmetic on things clerk.py already knows. Nothing
    in this function costs anything, which is the point of it: the expensive
    call happens only after this returns a reason, and the reason goes in
    the log so a server can see what it paid for.
    """
    stamp = _now(now)
    said, speakers = material.get("messages", 0), material.get("speakers", 0)
    if said >= MIN_MESSAGES and speakers >= MIN_SPEAKERS:
        return f"{said} messages from {speakers} people since the last look"

    for bill in material.get("open_bills", []):
        ends = _parse(bill.get("ends_at"))
        if ends is None or bill.get("waiting", 0) <= 0:
            continue
        if timedelta(0) < ends - stamp <= CLOSING_SOON:
            return (f"proposal no. {bill.get('no')} closes soon with "
                    f"{bill['waiting']} yet to vote")

    outstanding = material.get("outstanding", 0)
    if outstanding and material.get("outstanding_stale"):
        return f"{outstanding} decision(s) passed and not carried out"

    idle = _parse(material.get("floor_idle_since"))
    if idle and stamp - idle >= FLOOR_IDLE and said:
        return "nothing has been proposed in a while in a house that is talking"

    return None


# ---------- what he has already raised ----------

def slug(topic):
    return " ".join((topic or "").lower().split())[:80]


def raised_recently(topic, now=None):
    """Whether this is something he has already brought up lately."""
    key = slug(topic)
    if not key:
        return False
    when = _parse(_load().get("topics", {}).get(key))
    if when is None:
        return False
    return _now(now) - when < TOPIC_QUIET


def mark_raised(topic, now=None):
    key = slug(topic)
    if not key:
        return
    state = _load()
    stamp = _now(now)
    topics = state.get("topics", {})
    topics[key] = stamp.isoformat()
    cutoff = stamp - timedelta(days=LEDGER_DAYS)
    state["topics"] = {
        k: v for k, v in topics.items()
        if (_parse(v) or stamp) >= cutoff
    }
    _save(state)


def drop_topic(topic):
    """Forget that a topic was ever raised, so it can be raised again."""
    key = slug(topic)
    state = _load()
    if key in state.get("topics", {}):
        del state["topics"][key]
        _save(state)


# ---------- drafts waiting for somebody to want them ----------

def keep_draft(message_id, draft, topic, now=None):
    state = _load()
    drafts = state.get("drafts", {})
    drafts[str(message_id)] = {
        "title": draft.get("title", ""),
        "what": draft.get("what", ""),
        "why": draft.get("why", ""),
        "topic": topic or draft.get("title", ""),
        "at": _now(now).isoformat(),
    }
    # Only the recent ones are kept: an offer nobody took up two months ago
    # is not one to keep a button alive for.
    cutoff = _now(now) - timedelta(days=30)
    state["drafts"] = {
        k: v for k, v in drafts.items() if (_parse(v.get("at")) or _now(now)) >= cutoff
    }
    _save(state)


def draft(message_id):
    return _load().get("drafts", {}).get(str(message_id))


def drop_draft(message_id):
    state = _load()
    if str(message_id) in state.get("drafts", {}):
        del state["drafts"][str(message_id)]
        _save(state)
