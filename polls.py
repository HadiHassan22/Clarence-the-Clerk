"""Asking the whole server something, in a way that decides nothing.

There used to be one ballot with two audiences, and it was taken out on
purpose: two electorates, two quorum rules, two rooms, two refusals and two
ways for a vote to be settled, all threaded through one pipeline so that
every function about voting had to ask whose vote it was before it could do
anything. A `quorum` field meant something in one branch and nothing in the
other. That is the mistake this module exists not to repeat.

So a community poll is not a proposal, and nothing here is a proposal's.
It has its own store, its own room and its own close. It never earns a
number, never reaches the record, never carries, and the word for what
happens at the end of one is *reported*, not *decided*. The cooperative's
machinery is untouched by it: `bills.json` never learns the word poll,
`vote_state` is never asked about one, and there is still exactly one
meaning for the word carried.

The counting rule is different too, and has to be. A proposal counts
against the roster, where somebody signed up to be counted and silence is a
position. A whole server never signed up to anything, and most of a server
is quiet on any given day: counting silence as a no there would put every
poll out of reach on the morning it opened. So a poll is decided among the
people who answered it, and the quorum is the whole of the protection
against four people answering for six hundred.

Discord-free and pure standard library, like `roster.py` and `modules.py`,
so all of it is testable on a laptop. What needs to know about a server --
who is in it, which room this goes in -- is passed in.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import settings

# A draft nobody confirms is not a poll and must not become one later. The
# confirmation exists so that a poll going in front of the whole server is
# somebody's deliberate act, and a card sitting unanswered for a day and
# then pressed by whoever wanders past is that guarantee going quiet. Not a
# setting: no result turns on it, and a house that wants a slower door has
# not asked for one.
DRAFT_HOURS = 1

# What a poll can be, in the only order it can move through them. `draft`
# is written down rather than held in memory so that a restart between
# asking and confirming loses a card, which the confirm can rebuild, rather
# than losing the question somebody typed.
DRAFT, OPEN, CLOSED = "draft", "open", "closed"

# The bounds on a choice poll, and the reason they are here rather than in
# settings.py: they are the shape of a Discord message rather than a rule
# the house votes by. Five buttons to a row and two rows is the room a
# poll's card has to spare once the question is on it.
MIN_OPTIONS, MAX_OPTIONS = 2, 10

# What every poll's answer means where it is not one of the options. Yes/no
# is the shape a poll takes when nobody named any, and `abstain` is on both
# shapes because turning up to say nothing is an answer to a question about
# the room -- it is what tells a quorum apart from indifference.
YES, NO, ABSTAIN = "yes", "no", "abstain"


def _path(guild_id):
    return settings.state_file(guild_id, "polls.json")


def load(guild_id) -> list:
    """Every poll this server has, draft and open and closed.

    A store that will not parse reads as no polls rather than taking the
    daemon down with it: an unreadable poll is recoverable, a clerk that
    will not boot is not. Same bargain `settings.load` makes, for the same
    reason.
    """
    path = _path(guild_id)
    if path is None or not path.exists():
        return []
    try:
        got = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return got if isinstance(got, list) else []


def save(guild_id, polls):
    """Atomic: a redeploy mid-write must not truncate the store."""
    path = _path(guild_id)
    if path is None:
        return
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(polls, indent=2))
    tmp.replace(path)


def by_id(guild_id, poll_id):
    for poll in load(guild_id):
        if poll.get("id") == poll_id:
            return poll
    return None


def by_field(guild_id, field, value):
    """A poll by whichever of its message ids is in hand.

    Two of them, and they are not interchangeable: `message_id` is the card
    in the polls room that carries the ballot, `draft_message_id` is the
    confirmation card wherever it was asked for. A press on one must never
    find the other.
    """
    if value is None:
        return None
    for poll in load(guild_id):
        if poll.get(field) == value:
            return poll
    return None


def put(guild_id, poll):
    """Read-modify-write one poll. The caller holds the lock; this is the
    part that must not be written twice at two call sites."""
    polls = load(guild_id)
    for i, got in enumerate(polls):
        if got.get("id") == poll.get("id"):
            polls[i] = poll
            break
    else:
        polls.append(poll)
    save(guild_id, polls)
    return poll


def next_id(guild_id):
    """Monotonic, and never the length of the list: a swept draft would
    otherwise hand its number to the next poll, and two polls with one id
    is two cards answering each other's buttons."""
    return max((p.get("id", 0) for p in load(guild_id)), default=0) + 1


def draft(author_id, author, question, why=None, options=None):
    """A poll as it exists before anybody has agreed to put it up.

    It is not in a room, it has no window and it cannot be voted in. All it
    holds is what was typed and who typed it, so that the confirmation is
    agreeing to something specific rather than to the idea of a poll.
    """
    return {
        "id": None,
        "status": DRAFT,
        "question": question,
        "why": why or None,
        "options": list(options) if options else None,
        "author_id": author_id,
        "author": author,
        "created_at": _now().isoformat(),
        "ballots": {},
    }


def clean_options(raw):
    """The options as they will be voted on, or None for a yes/no poll.

    Blank entries are dropped rather than refused: a form with ten boxes in
    it is a form most people fill three of. Duplicates go too, because two
    buttons with one label is a poll whose result cannot be read.
    """
    if not raw:
        return None
    seen, out = set(), []
    for item in raw:
        text = str(item).strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out or None


def options_refusal(options):
    """Why this list of options cannot be voted on, or None if it can.

    Said as a sentence rather than returned as a bool, because both callers
    -- the modal and the tool -- have to tell somebody what to do about it,
    and two of them phrasing the same limit differently is how a house
    learns the rules are approximate.
    """
    if options is None:
        return None
    if len(options) < MIN_OPTIONS:
        return (f"A choice poll needs at least {MIN_OPTIONS} different "
                f"answers. Leave them out entirely for a yes/no question.")
    if len(options) > MAX_OPTIONS:
        return f"A poll can offer at most {MAX_OPTIONS} answers."
    return None


def answers(poll):
    """What the buttons on this poll are, in the order they are drawn."""
    return list(poll.get("options") or (YES, NO))


def may_answer(poll, choice):
    """Whether this is one of the things this poll can be answered with.

    Checked on the poll rather than on the button, for the same reason
    `may_vote` is: a stale card surviving a restart, or a message moved
    into another room, must not become a way to put an answer on a poll
    that never offered it.
    """
    return choice == ABSTAIN or choice in answers(poll)


def counts(poll):
    """Every answer with its count, including the ones nobody chose.

    Zero-filled so that reading a result never has to know which options
    happened to be picked -- a poll where one option got nothing says so,
    rather than silently not mentioning it.
    """
    tally = {a: 0 for a in answers(poll)}
    tally[ABSTAIN] = 0
    for choice in (poll.get("ballots") or {}).values():
        if choice in tally:
            tally[choice] += 1
    return tally


def cast(poll, user_id, choice):
    """Record one answer, or retract it when `choice` is None.

    Returns True if anything changed, so a caller can skip a rewrite of the
    card nobody moved.
    """
    ballots = poll.setdefault("ballots", {})
    uid = str(user_id)
    if choice is None:
        return ballots.pop(uid, None) is not None
    if ballots.get(uid) == choice:
        return False
    ballots[uid] = choice
    return True


def quorum(size, share):
    """How many have to answer for a poll to mean anything.

    Passage is a majority of whoever turned up, so this is the whole of the
    protection against a handful of people answering for a room. Never more
    than the room holds, and never zero while there is anybody at all to
    ask: a poll that reports a result on nobody's answer is not a poll, it
    is a press release.
    """
    if size <= 0:
        return 0
    return max(1, min(size, math.ceil(size * share)))


def needed(voted, share):
    """How many of the answers given carry a yes/no poll.

    Counted against the answers and not against the room, which is the
    difference between this and everything in `roster.py`, and the reason
    is in this module's docstring: a server never signed up to be counted,
    so silence here is silence rather than a no.

    Never less than a majority of the people who chose, and that floor is
    doing real work rather than echoing `roster.required`: a share of a
    half over ten answers is five, and five of ten is not what anybody
    reads "the room said yes" to mean -- it is a tie that reports as a
    result. So the bar is the same one an ordinary majority uses, and the
    share only ever raises it.
    """
    if voted <= 0:
        return 1
    majority = voted // 2 + 1
    return max(majority, min(voted, math.ceil(voted * share)))


def decided(poll, size, share, quorum_share):
    """What a poll came to, as a dict, or None if it is not closed yet.

    One function, so the card, the close and anything that reports a poll
    all read the same figures. Never called a pass or a fail: a poll is
    answered, and what the room said is the whole of the output.

    `abstain` is counted for the quorum and against nothing else. Somebody
    who turned up to say "I have no view" has turned up, and that is the
    question a quorum asks.
    """
    tally = counts(poll)
    voted = sum(tally.values())
    floor = quorum(size, quorum_share)
    result = {
        "counts": tally,
        "voted": voted,
        "room": size,
        "quorum": floor,
        "quorate": voted >= floor,
        "options": list(poll.get("options") or ()),
    }
    if not result["quorate"]:
        # Deliberately no winner, not even a leading one. A poll that
        # fell short of its quorum has not told the house anything, and
        # printing "yes was ahead" underneath that is the sentence people
        # would quote afterwards.
        result["answer"] = None
        return result
    if poll.get("options"):
        top = max((tally[o] for o in poll["options"]), default=0)
        leaders = [o for o in poll["options"] if tally[o] == top and top > 0]
        # A tie is reported as a tie. There is nothing here for a runoff to
        # protect -- no status quo, nothing carried -- so inventing a
        # second round would be asking the room twice to get a tidier
        # answer out of it.
        result["answer"] = leaders[0] if len(leaders) == 1 else None
        result["leaders"] = leaders
        result["top"] = top
        return result
    deciding = tally[YES] + tally[NO]
    bar = needed(deciding, share)
    result["needed"] = bar
    result["answer"] = YES if tally[YES] >= bar and deciding else NO
    return result


def _now():
    return datetime.now(timezone.utc)


def open_poll(poll, poll_id, hours, channel_id, message_id):
    """A confirmed draft becoming a poll in a room, in one place.

    The window is stamped here rather than at the draft, because a draft
    that sat for fifty minutes should not open with ten minutes left on it.
    """
    poll["id"] = poll_id
    poll["status"] = OPEN
    poll["channel_id"] = channel_id
    poll["message_id"] = message_id
    poll["opened_at"] = _now().isoformat()
    poll["ends_at"] = (_now() + timedelta(hours=hours)).isoformat()
    return poll


def close(poll, result):
    """Shut a poll where it stands and destroy the answers.

    The counts survive and the ballots do not, which is the same promise
    every ballot in this building makes: a closed poll cannot be
    reconstructed into who said what, by anybody, including him.
    """
    poll["status"] = CLOSED
    poll["closed_at"] = _now().isoformat()
    poll["result"] = result
    poll.pop("ballots", None)
    return poll


def _at(poll, field):
    raw = poll.get(field)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def is_over(poll, at=None):
    """Whether an open poll's window has run out.

    A poll never ends early. Its denominator is whoever answers, so every
    answer still to come moves the bar underneath one already met -- there
    is no count at which the result can no longer change, which is exactly
    the property the cooperative's early close is built on and the one a
    turnout rule does not have.
    """
    if poll.get("status") != OPEN:
        return False
    ends = _at(poll, "ends_at")
    if ends is None:
        # A poll with no window is a bug, not a poll that runs for ever.
        return True
    return ends <= (at or _now())


def stale_draft(poll, at=None, hours=DRAFT_HOURS):
    """Whether an unconfirmed draft has sat long enough to be swept."""
    if poll.get("status") != DRAFT:
        return False
    made = _at(poll, "created_at")
    if made is None:
        return True
    return (at or _now()) - made > timedelta(hours=hours)


def sweep(guild_id, at=None):
    """Drop the drafts nobody confirmed. Returns how many went.

    Kept out of the close loop's way on purpose: an abandoned draft is not
    a poll that failed, it is a question that was never asked, and it
    leaves no trace for the same reason an unsent message does not.
    """
    polls = load(guild_id)
    keep = [p for p in polls if not stale_draft(p, at)]
    if len(keep) != len(polls):
        save(guild_id, keep)
    return len(polls) - len(keep)


def due(guild_id, at=None):
    """Every open poll whose window has run out."""
    return [p for p in load(guild_id) if is_over(p, at)]


def open_polls(guild_id):
    return [p for p in load(guild_id) if p.get("status") == OPEN]
