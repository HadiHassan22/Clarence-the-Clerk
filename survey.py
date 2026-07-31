"""The long look: what is broken here, what is missing, what wants tidying.

Everything else the clerk does is answering. This is the one thing he does
by looking: somebody says *what needs cleaning* or *what have we not
finished* and gets an answer about their actual server rather than a
paragraph of good advice that would be true anywhere.

The whole of the looking is free. Every rule below is plain Python over
facts already in memory -- no model, no tokens, no network -- and the list
that comes out is the same list whether or not the server has ever bought
a key. What a brain adds is judgement on top: which three of the nineteen
findings are worth anybody's afternoon, and in what order. That is a good
use of an expensive model and a waste of a cheap one, which is why it is
asked for rather than assumed.

Discord-free, like `warden.py` and `modules.py`. `clerk.py` walks the
guild and hands over a plain dictionary of facts; every rule in here is a
function of that dictionary, so the whole audit runs on a laptop against a
made-up server. The alternative -- rules that reach into `discord.Guild`
themselves -- is an audit nobody can test, which is an audit nobody should
trust.

Findings are graded, and the grades mean something specific:

- **broken**  something cannot work at all until somebody acts.
- **wrong**   it works, but not the way the house thinks it does. Two of
              something, a binding pointing at the empty one, a register
              that has drifted from reality.
- **untidy**  it works and it is correct and there is cruft. Nobody has to
              do anything about these, ever, and saying so is the point:
              an audit that grades everything urgent is one people learn
              to close.
- **missing** nothing is wrong; something has simply never been set up.

A rule that cannot decide returns nothing. Silence is a finding's absence,
never a guess: `unknown` is not a grade, and inventing a problem to fill a
category is how a report stops being read.
"""

from __future__ import annotations

import logging

log = logging.getLogger("survey")

GRADES = ("broken", "wrong", "untidy", "missing")

MARKS = {"broken": "🔴", "wrong": "🟠", "untidy": "🧹", "missing": "⬜"}

# How long a room has to be quiet before it is worth mentioning. Not a
# rule about how servers should be used -- plenty of rooms are seasonal --
# just the point past which "is this still wanted" is a fair question.
QUIET_DAYS = 60

# A floor nobody has filed on for this long, in a server that is otherwise
# talking, is the one finding here that is about the parliament rather than
# the furniture.
IDLE_FLOOR_DAYS = 21


def _f(grade, key, what, why=None, fix=None, items=()):
    return {"grade": grade, "key": key, "what": what, "why": why, "fix": fix,
            "items": list(items)}


# ---------- the rules ----------
#
# Each takes the facts and yields findings. Split one per subject rather
# than one long walk, because the useful thing to be able to say about an
# audit is "that rule is wrong" and then fix one function.

def _permissions(facts):
    me = facts.get("me") or {}
    lacking = me.get("missing_permissions") or []
    if lacking:
        yield _f("broken", "permissions",
                 "I am missing " + ", ".join(lacking) + ".",
                 "Nothing that makes or changes a channel or a role will "
                 "work until that is granted.",
                 "Server Settings → Roles → my role, or re-invite me with "
                 "the link install.py printed.")
    blocked = me.get("outranked_by") or []
    if blocked:
        yield _f("broken", "role_position",
                 f"{len(blocked)} role(s) sit above mine.",
                 "Discord will not let me touch anybody holding one of "
                 "them, whatever the house has decided.",
                 "Drag my role to the top of the role list.",
                 items=blocked)
    if me.get("message_content") is False and facts.get("chat_on"):
        yield _f("broken", "intent",
                 "Conversation is on, but this host runs without the "
                 "Message Content intent.",
                 "I cannot read a word anybody says, so mentioning me does "
                 "nothing and the filters see empty messages.",
                 "Developer Portal → Bot → Privileged Gateway Intents, or "
                 "switch conversation off so the panel stops claiming it "
                 "works.")


def _stranded(facts):
    held = (facts.get("slate") or {}).get("stranded_from")
    if held:
        yield _f("broken", "stranded",
                 f"The record on this disk was written by server {held}.",
                 "Its proposals, its decisions and its numbering are being "
                 "read as this server's.",
                 "`/setup` → Start fresh, or point CLERK_DATA_DIR somewhere "
                 "of this server's own.")


def _modules(facts):
    """Features switched on and not running, grouped by what they are
    waiting for.

    Grouped rather than listed, because three features waiting on one
    missing key is one fact and three lines of it reads as three problems.
    The report is meant to be exhaustive about *facts*, not about
    restatements of the same one.
    """
    keyed = bool((facts.get("brain") or {}).get("provider"))
    waiting = {}
    for key, state in (facts.get("modules") or {}).items():
        if state.get("state") != "dormant":
            continue
        for why in state.get("blockers") or ["something"]:
            # "no AI key" is already a finding of its own, once. A server
            # that has never bought one does not need to be told three more
            # times under three feature names.
            if why == "no AI key" and not keyed:
                continue
            waiting.setdefault(why, []).append(state["name"])
    for why, names in waiting.items():
        yield _f("broken", "dormant." + why.strip("`").replace(" ", "_"),
                 (f"{names[0]} is switched on and not running."
                  if len(names) == 1 else
                  f"{len(names)} features are switched on and not running."),
                 why,
                 "`/setup` → Apply builds what it is waiting for.",
                 items=names if len(names) > 1 else ())

    for key, state in (facts.get("modules") or {}).items():
        if state.get("state") == "blocked":
            yield _f("wrong", f"blocked.{key}",
                     f"{state['name']} is switched on but stands on "
                     f"something that is off.",
                     "; ".join(state.get("blockers") or []) or None,
                     "Switch that one on too, or switch this one off so the "
                     "list stops claiming it.")


def _duplicate_rooms(facts):
    """Two channels whose names both claim the same job.

    The oldest bug in this codebase and the one worth catching for good:
    a server built from a terminal and then set up again from inside
    Discord ended up with two of every governance room, and the binding
    pointed at whichever was made second, which was the empty one.
    """
    claims = {}
    for channel in facts.get("channels") or []:
        job = channel.get("claims")
        if job:
            claims.setdefault(job, []).append(channel)
    bound = (facts.get("bindings") or {}).get("rooms") or {}
    for job, found in claims.items():
        if len(found) < 2:
            continue
        names = [f"#{c['name']}" for c in found]
        pointed = bound.get(job)
        which = next((c for c in found if c["id"] == pointed), None)
        yield _f("wrong", f"duplicate.{job}",
                 f"{len(found)} channels look like the `{job}` room.",
                 (f"I use {'#' + which['name'] if which else 'none of them'}"
                  + (f", which has {which['messages']} message(s) in it."
                     if which and which.get("messages") is not None
                     else ".")),
                 "Point me at the right one under `/setup` → Rooms, and "
                 "archive the other.",
                 items=names)


def _bindings(facts):
    bound = (facts.get("bindings") or {}).get("rooms") or {}
    unreachable = [job for job, info in (facts.get("room_health") or {}).items()
                   if info.get("cannot_post")]
    if unreachable:
        yield _f("broken", "unreachable",
                 "I cannot post in " + ", ".join(f"`{j}`" for j in unreachable)
                 + ".",
                 "The room is bound and I am not allowed to speak in it, "
                 "which looks exactly like a bot that has stopped working.",
                 "Give my role Send Messages there, or bind the job "
                 "somewhere else.")
    off_but_bound = [job for job, owner in (facts.get("room_owners") or {}).items()
                     if bound.get(job) and owner and not owner.get("enabled")]
    if off_but_bound:
        yield _f("untidy", "bound_but_off",
                 "Bound to rooms nothing uses: "
                 + ", ".join(f"`{j}`" for j in off_but_bound) + ".",
                 "The feature that would post there is switched off.",
                 "Harmless. Switch the feature on, or clear the binding "
                 "under `/setup` → Rooms.")


def _cooperative(facts):
    coop = facts.get("cooperative") or {}
    size = coop.get("size")
    if size == 0:
        yield _f("broken", "coop_empty",
                 "Nobody is in the cooperative.",
                 "I refuse everyone, including whoever installed me.",
                 "`/setup` → Apply puts you in.")
    elif size == 1:
        yield _f("missing", "coop_one",
                 "One person is in the cooperative.",
                 "Every threshold is measured against the roster, so one "
                 "person is a unanimous parliament of one.",
                 "`/setup` → Roles & votes hands it to a few more. That is "
                 "the only way onto this roll — `/invite` is the server's "
                 "door and puts nobody on it.")
    away = coop.get("away") or 0
    if size and away and away >= max(2, size // 2):
        yield _f("wrong", "coop_away",
                 f"{away} of {size} in the cooperative are counted as away.",
                 "They are out of the denominator, so thresholds are being "
                 "measured against a smaller house than the roll suggests.",
                 "Nothing to do if that is right. If it is not, `away_days` "
                 "under `/setup` → Numbers is the dial.")


def _record(facts):
    record = facts.get("record") or {}
    outstanding = record.get("outstanding") or []
    if outstanding:
        yield _f("missing", "outstanding",
                 f"{len(outstanding)} decision(s) passed and not carried out.",
                 "The house voted for these and nothing has happened since.",
                 "Tell me when one is done and it comes off the list.",
                 items=outstanding[:8])
    stale = record.get("stale_open") or []
    if stale:
        yield _f("wrong", "stale_floor",
                 f"{len(stale)} vote(s) are past their window and still open.",
                 "They should have closed themselves. If this list is not "
                 "empty, something stopped the clock.",
                 "`/close <number>` calls time on one by hand.",
                 items=stale[:8])
    quiet = record.get("floor_quiet_days")
    if (quiet is not None and quiet >= IDLE_FLOOR_DAYS
            and (facts.get("guild") or {}).get("talking")):
        yield _f("missing", "floor_idle",
                 f"Nothing has been filed for {quiet} days.",
                 "The server is talking and the floor is empty, which is "
                 "either contentment or a parliament nobody remembers "
                 "having.",
                 "Not a problem to fix. Worth knowing.")


def _rooms_missing(facts):
    wanted = facts.get("optional_unbound") or []
    if wanted:
        yield _f("missing", "optional_rooms",
                 "Never pointed at a room: " + ", ".join(f"`{j}`" for j in wanted)
                 + ".",
                 "Each of those features works without one; they are better "
                 "with.",
                 "`/setup` → Rooms.")


def _clutter(facts):
    empty = [c["name"] for c in (facts.get("categories") or [])
             if not c.get("channels")]
    if empty:
        yield _f("untidy", "empty_categories",
                 f"{len(empty)} empty category(ies).",
                 None, "Delete them, or do not. Nothing reads them.",
                 items=empty)

    quiet = [f"#{c['name']} ({c['quiet_days']}d)"
             for c in (facts.get("channels") or [])
             if c.get("quiet_days") is not None
             and c["quiet_days"] >= QUIET_DAYS
             and not c.get("claims") and not c.get("archived")]
    if quiet:
        yield _f("untidy", "quiet_channels",
                 f"{len(quiet)} channel(s) with nothing said in "
                 f"{QUIET_DAYS}+ days.",
                 "Plenty of rooms are seasonal. This is a question, not a "
                 "complaint.",
                 "Archive the ones that are finished.",
                 items=quiet[:12])

    archived = [c["name"] for c in (facts.get("channels") or [])
                if c.get("archived")]
    if len(archived) >= 8:
        yield _f("untidy", "archive_full",
                 f"{len(archived)} channels sit in the archive.",
                 "Debate rooms from before they became threads; nothing "
                 "clears them, and no more arrive.",
                 "Delete the oldest if the record in the decisions room is "
                 "the part you wanted to keep.",
                 items=archived[:10])


def _colours(facts):
    colours = facts.get("colours") or {}
    gone = colours.get("registered_but_gone") or []
    if gone:
        yield _f("wrong", "colour_ghosts",
                 f"{len(gone)} colour role(s) on the register no longer exist.",
                 "Somebody deleted the role by hand. The register still "
                 "counts them against its owner's allowance.",
                 "Harmless, and I clear them as I notice them.",
                 items=gone[:10])
    orphaned = colours.get("owner_left") or []
    if orphaned:
        yield _f("untidy", "colour_orphans",
                 f"{len(orphaned)} colour role(s) belong to people who have "
                 f"left.",
                 "Anybody can still wear them; nobody can edit them.",
                 "Delete them, or leave them as a wardrobe.",
                 items=orphaned[:10])
    unworn = colours.get("unworn") or []
    if len(unworn) >= 5:
        yield _f("untidy", "colour_unworn",
                 f"{len(unworn)} colour role(s) nobody is wearing.",
                 None, "Cleaning, not correctness.",
                 items=unworn[:10])


def _brain(facts):
    brain = facts.get("brain") or {}
    if not brain.get("provider"):
        yield _f("missing", "no_key",
                 "No AI key, so I keep records and hold the door and say "
                 "nothing.",
                 None, "`/setup` → Brain.")
        return
    spent, budget = brain.get("spent") or 0.0, brain.get("budget") or 0.0
    if budget and spent >= budget * 0.8:
        yield _f("wrong", "budget",
                 f"${spent:.2f} of ${budget:.0f} spent this month.",
                 "Past 75% the heartbeat stops entirely, so that he can "
                 "still answer when spoken to.",
                 "Raise the budget, or leave it: it resets on the first.")
    if brain.get("deep_better") is False:
        yield _f("missing", "flat_deep_model",
                 "The long look would use the same model as an ordinary "
                 "reply.",
                 "This report is free either way, and stays exactly as it "
                 "is. What a better model adds is judgement over the whole "
                 "of it at once, which is the one job here where the choice "
                 "changes the answer rather than the wording.",
                 "`/setup` → Brain, third field.")


def _greetings(facts):
    """Two greetings for one arrival.

    Discord has its own join notices, on by default in the system channel,
    and plenty of servers already run something for this. The clerk cannot
    see another bot, but it can see Discord's own switch, and that is the
    common half of the problem. Saying it is the whole fix: nobody wants
    two hellos, and which one to keep is theirs to decide.
    """
    arrivals = facts.get("arrivals") or {}
    if not arrivals.get("on"):
        return
    if arrivals.get("discord_greets") and arrivals.get("room"):
        yield _f("wrong", "double_greeting",
                 "Two things greet every arrival here.",
                 f"Discord posts its own join notice in "
                 f"#{arrivals['discord_room']}, and I greet people in "
                 f"#{arrivals['room']}.",
                 "Keep one. Server Settings → Overview turns Discord's off; "
                 "switching Arrivals off in `/setup` turns mine off.")
    elif arrivals.get("discord_greets") and not arrivals.get("room"):
        yield _f("untidy", "discord_greets",
                 f"Discord already greets arrivals in "
                 f"#{arrivals['discord_room']}.",
                 "Arrivals is switched on here but has no room, so I am not "
                 "adding a second hello. Nothing is wrong.",
                 "Leave it. Or switch Arrivals off so the list stops "
                 "mentioning it at all.")


def _house(facts):
    if not (facts.get("guild") or {}).get("described"):
        yield _f("missing", "house",
                 "Nobody has said what this server is for.",
                 "I read the name off Discord. The point of the place only "
                 "its people know, and without it I describe it neutrally.",
                 "`/setup` → What this place is.")


RULES = (_permissions, _stranded, _modules, _duplicate_rooms, _bindings,
         _cooperative, _record, _rooms_missing, _clutter, _colours, _brain,
         _greetings, _house)


# ---------- the report ----------

def inspect(facts):
    """Every finding, worst first. Free, and the same list whether or not
    this server has ever bought a key."""
    found = []
    for rule in RULES:
        try:
            found.extend(rule(facts) or ())
        except Exception as e:  # one bad rule must not lose the other eleven
            log.error(f"survey rule {rule.__name__} failed: {e!r}")
    return sorted(found, key=lambda f: GRADES.index(f["grade"]))


def tally(findings):
    return {grade: sum(1 for f in findings if f["grade"] == grade)
            for grade in GRADES}


def headline(findings):
    counts = tally(findings)
    if not findings:
        return "Nothing to report. Everything I can check is as it should be."
    if counts["broken"]:
        return (f"**{counts['broken']} thing(s) cannot work** until somebody "
                f"acts.")
    if counts["wrong"]:
        return f"Nothing is broken. **{counts['wrong']} thing(s) are not what "\
               f"the house thinks they are.**"
    if counts["untidy"] and not counts["missing"]:
        return "Nothing is wrong. There is some cruft."
    return "Nothing is broken. Some things have never been set up."


def render(findings, limit=1900):
    """The report as a human reads it, grouped by grade and trimmed to fit
    one Discord message."""
    if not findings:
        return headline(findings)
    lines = [headline(findings), ""]
    for grade in GRADES:
        block = [f for f in findings if f["grade"] == grade]
        if not block:
            continue
        lines.append(f"### {MARKS[grade]} {grade}")
        for finding in block:
            lines.append(f"**{finding['what']}**")
            if finding.get("why"):
                lines.append(finding["why"])
            if finding.get("items"):
                lines.append("-# " + ", ".join(str(i) for i in finding["items"]))
            if finding.get("fix"):
                lines.append(f"-# → {finding['fix']}")
        lines.append("")
    out = "\n".join(lines).rstrip()
    if len(out) <= limit:
        return out
    # Trim from the bottom, which is the untidy end, and say so rather than
    # cutting mid-sentence and leaving somebody to wonder.
    kept, size = [], 0
    for line in lines:
        if size + len(line) + 1 > limit - 90:
            break
        kept.append(line)
        size += len(line) + 1
    kept.append("")
    kept.append("-# Trimmed to fit one message. Ask me about any grade for "
                "the rest.")
    return "\n".join(kept)


def brief(findings):
    """The same list, flattened for a model to read and judge. Shorter than
    `render` on purpose: the marks, the blank lines and the arrows are for
    a person."""
    return [
        {k: v for k, v in f.items() if v not in (None, [], "")}
        for f in findings
    ]
