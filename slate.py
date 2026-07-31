"""Wiping a server's slate, and knowing whose slate it is.

Half of what the clerk remembers is already kept per server, in
`guilds/<id>/`, so pointing him at a second house gives that house its own
settings, its own keys, its own warnings and cases. The other half is not:
the record, the roster, what he remembers, the ledgers of what he has said
all sit at the top of the data directory, from the days when there was
only ever one house. Move the daemon to a new server and it arrives
carrying the last one's proposals, its decisions, its numbering, and forty
notes about people who are not there.

So two things live here. **Whose slate it is**: the data directory is
stamped with the guild it belongs to, and a mismatch is said out loud
rather than discovered later in a room full of somebody else's history.
And **clearing it**, by scope, because "start again" means different
things: a server that wants its bill numbers back at one is not asking to
be forgotten as a person, and one asking to be forgotten is not asking to
lose its decisions.

Nothing here imports Discord and nothing here decides anything. It says
what would go, deletes what it is told to, and reports what it did.

Two rules:

**It never touches a key.** An AI key is the one thing in the store that
costs money to replace and cannot be reconstructed from Discord. Clearing
a server's settings clears its bindings, its switches and its numbers, and
leaves every key exactly where it was.

**It never deletes anything in Discord.** Channels, roles and messages are
the server's, not the clerk's. Forgetting the registry of colour roles
does not delete the roles: it means he no longer knows who made them, and
the reply says so rather than letting somebody find out.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import settings

log = logging.getLogger("slate")

_root = None

# The one file that is bookkeeping rather than record: pinned message ids,
# the bill counter, the commit last announced.
STATE = "clerk_state.json"


def configure(data: Path):
    global _root
    _root = Path(data)


def root() -> Path:
    if _root is None:
        raise RuntimeError("slate.configure() was never called")
    return _root


# ---------- what may be cleared ----------
#
# shared: files at the top of the data directory, from before there was
#         more than one house. These are the ones that follow the daemon to
#         a new server, and the reason this module exists.
# owned:  files already kept per guild. Clearing them is tidiness rather
#         than necessity, since a new server gets its own directory anyway.
# keys:   individual entries inside clerk_state.json, for the scopes that
#         want a counter back at zero without throwing away pinned messages.
# costs:  said out loud before anything happens. Written in the second
#         person, because somebody is about to read it and decide.

SCOPES = {
    "record": {
        "name": "The record",
        "blurb": "Proposals, decisions, and the numbering.",
        "costs": "Every proposal ever filed and every decision ever passed. "
                 "Numbering starts again at 1. The posts already in the "
                 "decisions room stay where they are; he simply no longer "
                 "knows about them.",
        "shared": ("bills.json", "acts.json"),
        "owned": (),
        "keys": ("bill_counter",),
    },
    "memory": {
        "name": "What he knows",
        "blurb": "His notes on people, the house shelf, and the room he has "
                 "been listening to.",
        "costs": "Every note he holds on every person, and the house lore "
                 "he has filed. He starts again from nothing, which is also "
                 "what `/whatdoyouknow forget: True` does for one person.",
        # Both shelves are one file now. The other two are what an older
        # install left behind -- the book itself, and the copy the migration
        # set aside on the way past -- and they are listed because a wipe
        # that leaves yesterday's memories readable on disk has not wiped
        # anything.
        "shared": ("people.json", "clerk_memory.json",
                   "clerk_memory.json.migrated"),
        "owned": (),
        "keys": (),
    },
    "chatter": {
        "name": "What he has said",
        "blurb": "The ledgers that stop him saying the same thing twice.",
        "costs": "The record of nudges sent, roster changes announced and "
                 "topics the heartbeat has raised. Harmless to clear, except "
                 "that things already said once may be said once more.",
        "shared": ("duties.json", "pulse.json"),
        "owned": (),
        "keys": (),
    },
    "roster": {
        "name": "The roster",
        "blurb": "Who has been about lately, and who made which colour.",
        "costs": "Everyone's last-seen time, so the whole roster counts as "
                 "present until people speak again, and the register of who "
                 "made which colour role. **The roles themselves are not "
                 "deleted** — he just stops knowing whose they are, and "
                 "nobody can edit theirs until they make another.",
        "shared": ("roster.json", "roles.json"),
        "owned": (),
        "keys": (),
    },
    "moderation": {
        "name": "The case book",
        "blurb": "Warnings, cases, and saved answers.",
        "costs": "Every warning and every case on record, and the shelf of "
                 "saved answers.",
        "shared": (),
        # warden_levels.json and warden_stars.json were here until levels
        # and the starboard were removed. A server that ran either still
        # has the file; nothing reads it, and nothing here pretends to be
        # able to clear a feature that no longer exists.
        "owned": ("warden_cases.json", "warden_tags.json"),
        "keys": (),
    },
    "furniture": {
        "name": "His own posts",
        "blurb": "The message ids of the things he keeps pinned and current.",
        "costs": "Nothing, except that he posts a fresh health card, a fresh "
                 "propose button and a fresh wardrobe rather than editing the "
                 "ones he had. Use it when those are pointing at messages "
                 "somebody deleted.",
        "shared": (),
        "owned": (),
        "keys": ("health_message_id", "outstanding_message_id",
                 "wardrobe_message_id", "bill_message_id", "roles_home_id",
                 "announced_commit"),
    },
    "install": {
        "name": "The install",
        "blurb": "Bindings, feature switches, voting numbers, and what this "
                 "place is.",
        "costs": "Which channel does which job, which role votes, which "
                 "features are on, the numbers this house votes by, and its "
                 "one line about itself. **AI keys are not touched.** You "
                 "will want to run `/setup` again straight afterwards.",
        "shared": (),
        "owned": (),
        "keys": (),
        "config": ("rooms", "roles", "categories", "modules", "voting",
                   "house"),
    },
}

# What a fresh install into a different server has to lose, and nothing
# more. The install itself is left alone: somebody halfway through pointing
# the clerk at a new house should not have to bind every room twice.
ON_MOVE = ("record", "memory", "chatter", "roster", "moderation", "furniture")

# Two things are deliberately not on the list above, and neither is an
# oversight:
#
# `logs/executor_log.json` -- the audit log. Every tool the model has ever
# been dispatched, who asked for it and what came back. An audit log with a
# button that empties it is not an audit log, and the one place it would be
# most useful is exactly the situation where somebody wants it gone. It is
# a file on a disk; whoever has the disk can delete it, and that is a
# different act from pressing a button in Discord.
#
# `brain_state.json` -- what a server has spent this month. Kept per guild,
# so a new server starts at zero without anybody clearing anything, and
# zeroing it for the server that actually spent the money would make the
# budget a suggestion.


def keys():
    return list(SCOPES)


def name(scope):
    got = SCOPES.get(scope)
    return got["name"] if got else scope


# ---------- whose slate this is ----------

def owner():
    """The guild this data directory belongs to, or None if it has never
    been claimed. Read straight off disk rather than through `settings`,
    which is keyed by the guild and so cannot answer this question."""
    try:
        state = json.loads((root() / STATE).read_text())
    except (OSError, json.JSONDecodeError, RuntimeError):
        return None
    got = state.get("guild_id")
    return int(got) if got else None


def claim(guild_id):
    """Stamp the directory. Called once a server is actually being served,
    so an unclaimed directory means nobody has ever run here."""
    path = root() / STATE
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get("guild_id") == int(guild_id):
        return False
    state["guild_id"] = int(guild_id)
    _write_state(state)
    log.info(f"data directory claimed by guild {guild_id}")
    return True


def stranded(guild_id):
    """Whether this directory holds a different server's history.

    Not a crash and not an automatic wipe. A wrong `GUILD_ID` is a typo a
    person can fix in a minute; a record deleted because of one is not
    recoverable, and choosing to erase somebody's parliament on their
    behalf is not a call this gets to make.
    """
    held = owner()
    return held is not None and held != int(guild_id)


def has_history():
    """Whether anything is written down at the top of the directory yet.

    What an installer asks before offering to clear: a first install has
    nothing to clear and should not be asked about it. Only the shared
    files, because those are the ones that follow the daemon; a previous
    server's own directory is not in anybody's way.
    """
    return any((root() / f).exists()
               for scope in ON_MOVE for f in SCOPES[scope]["shared"])


# ---------- what is there ----------

def _write_state(state):
    path = root() / STATE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _count(path: Path):
    """Roughly how much is in a file, for saying what is about to go.
    Never raises: this is a courtesy line, not a computation."""
    try:
        got = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return len(got) if isinstance(got, (list, dict)) else None


def present(guild_id, scope):
    """What of this scope actually exists: {"shared": [...], "owned": [...],
    "keys": [...], "config": [...]}, each a list of (name, count)."""
    spec = SCOPES.get(scope)
    if spec is None:
        return {"shared": [], "owned": [], "keys": [], "config": []}
    found = {"shared": [], "owned": [], "keys": [], "config": []}
    for filename in spec["shared"]:
        path = root() / filename
        if path.exists():
            found["shared"].append((filename, _count(path)))
    for filename in spec["owned"]:
        # `peek`, never `state_file`: looking must not create anything.
        path = settings.peek(guild_id, filename)
        if path is not None:
            found["owned"].append((filename, _count(path)))
    if spec["keys"]:
        try:
            state = json.loads((root() / STATE).read_text())
        except (OSError, json.JSONDecodeError, RuntimeError):
            state = {}
        found["keys"] = [(k, None) for k in spec["keys"] if k in state]
    for field in spec.get("config", ()):
        try:
            if settings.get(guild_id, field) is not None:
                found["config"].append((field, None))
        except RuntimeError:
            break
    return found


def summary(guild_id, scopes=None):
    """One line per scope, for a screen somebody decides from."""
    lines = []
    for scope in (scopes or keys()):
        spec = SCOPES[scope]
        found = present(guild_id, scope)
        total = sum(len(v) for v in found.values())
        counted = [f"{n}" + (f" ({c})" if c else "")
                   for group in ("shared", "owned") for n, c in found[group]]
        lines.append(
            f"**{spec['name']}** — {spec['blurb']}\n"
            + (f"-# {', '.join(counted)}" if counted else
               "-# nothing written yet" if not total else
               f"-# {total} thing(s) stored")
        )
    return "\n".join(lines)


# ---------- clearing it ----------

def wipe(guild_id, scopes):
    """Clear the named scopes. Returns what went, by scope.

    Deletes files rather than emptying them, because every loader in the
    clerk already reads a missing file as "nothing yet" -- that is how a
    first run works -- and a file that is not there cannot be half-read.
    """
    scopes = [s for s in (scopes or ()) if s in SCOPES]
    gone = {}
    state_keys = []
    config_fields = []
    for scope in scopes:
        spec = SCOPES[scope]
        cleared = []
        for filename in spec["shared"]:
            path = root() / filename
            if path.exists():
                path.unlink()
                cleared.append(filename)
        for filename in spec["owned"]:
            path = settings.peek(guild_id, filename)
            if path is not None:
                path.unlink()
                cleared.append(filename)
        state_keys.extend(spec["keys"])
        config_fields.extend(spec.get("config", ()))
        if cleared:
            gone[scope] = cleared

    # The bookkeeping file is edited rather than deleted: the guild stamp
    # lives in it, and losing that would make the directory look unclaimed
    # the moment after somebody deliberately cleared it.
    if state_keys:
        try:
            state = json.loads((root() / STATE).read_text())
        except (OSError, json.JSONDecodeError):
            state = {}
        dropped = [k for k in state_keys if state.pop(k, None) is not None]
        if dropped:
            _write_state(state)
            for scope in scopes:
                hit = [k for k in SCOPES[scope]["keys"] if k in dropped]
                if hit:
                    gone.setdefault(scope, []).extend(hit)

    if config_fields:
        dropped = [f for f in config_fields
                   if settings.get(guild_id, f) is not None]
        if dropped:
            settings.drop(guild_id, *dropped)
            for scope in scopes:
                hit = [f for f in SCOPES[scope].get("config", ())
                       if f in dropped]
                if hit:
                    gone.setdefault(scope, []).extend(hit)

    if gone:
        log.warning(
            f"slate wiped for guild {guild_id}: "
            + "; ".join(f"{s}: {', '.join(v)}" for s, v in gone.items())
        )
    return gone
