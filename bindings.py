"""Where each room is, and which role means what, in this server.

Everything is bound by id, never by name. Renaming a channel, dropping the
emoji in front of it, or moving it to another category changes nothing, because
none of those change its id. A server is described entirely by what it points
at, so no server has to be laid out like any other one.

This replaces matching on names, which needed base-name stripping, a substring
fallback, an alias table for renames and a list of former names in the config
-- five mechanisms all compensating for the same wrong decision.

Nothing here raises when something is unbound. A missing binding means the
feature that needs it is dormant and says so; it never means a crash.
"""

from __future__ import annotations

import logging

import settings

log = logging.getLogger("bindings")

# The jobs a channel can hold. The key is what the code asks for; the text is
# what a human picking it in the setup menu reads.
ROOMS = {
    "proposals": "Where a new proposal is announced",
    "votes": "Where ballots run",
    "decisions": "The permanent record of what was decided",
    "polls": "Questions put to the whole server, deciding nothing",
    "chat": "Where people talk to Eugene. He makes this one himself",
}

# Rooms without which governance cannot run at all.
#
# Kept for the one question that predates modules -- can this server hold a
# vote -- but which rooms matter now depends on what is switched on, and
# `modules.required_rooms()` is the answer to that. A server running Eugene
# as a moderator with governance off is not a broken server.
ESSENTIAL_ROOMS = ("votes", "decisions")

ROLES = {
    "cooperative": "Holds a vote",
    "member": "In the room, without a vote",
    # Held by whoever wants telling, and by nobody else. It decides nothing
    # and admits nobody to anything: the only thing it changes is whether a
    # new ballot arrives in your notifications.
    "bell": "Rung when a ballot opens",
}

# Categories Eugene files things under, rather than posts into. `chambers`
# used to be here, for the category a vote built its debate rooms in; the
# debate is a thread on the floor now and builds nothing, so the key went
# with the rooms rather than staying on the panel as a job nobody has.
CATEGORIES = {
    "archive": "Where the debate rooms of older votes are filed",
}

# What a room called this is for. Keyed by base name -- the channel without
# its emoji prefix -- because the layout file writes "🗳️・votes" and a person
# typing the same thing writes "votes", and those have to be one room.
#
# Former names are in here too, so a server built before a rename is adopted
# rather than duplicated. This table exists because there were two routes into
# a fresh server -- `build_server.py` from a terminal and `/setup` from
# inside Discord -- and only one of them knew what the rooms were called. The
# other matched on the bare word, found nothing, and helpfully made a second
# #votes next to the first.
JOBS = {
    "propose": "proposals",
    "submit-a-bill": "proposals",
    "proposals": "proposals",
    "votes": "votes",
    "the-floor": "votes",
    "decisions": "decisions",
    "gazette": "decisions",
    "community-polls": "polls",
    # The bare word too, because a server that had the old polls room -- or
    # simply has one called `polls` -- means the same room by it, and the
    # alternative is Eugene building a second one alongside it.
    "polls": "polls",
    # His own room, by his own name, and only by that. A server's existing
    # `#chat` is not on this list and must never be: binding `chat` does not
    # add a room he talks in, it takes away every other one, and no server
    # asked for that by naming a channel after the ordinary English word.
    "eugene-chat": "chat",
}

_ROOMS_KEY = "rooms"
_ROLES_KEY = "roles"
_CATS_KEY = "categories"


def base_name(name):
    """Channel identity ignoring the emoji prefix: '🗳️・votes' -> 'votes'."""
    return str(name).split("・", 1)[-1].strip().lower()


def job_of(channel_name):
    """The job a channel's name says it does, or None."""
    return JOBS.get(base_name(channel_name))


def adopt(guild_id, channel):
    """Bind a channel to the job its name announces, unless that job is
    already spoken for. Returns the job it took, or None.

    Deliberately meek: it never re-points a job somebody has already bound
    by hand, because a name is a guess and a binding is a decision. It is
    also entirely optional -- a caller with no settings store yet (the
    terminal builder on a machine that has never run the bot) simply gets
    None rather than an exception, and the rooms still get built.
    """
    if channel is None:
        return None
    key = job_of(getattr(channel, "name", ""))
    if key is None:
        return None
    try:
        if _table(guild_id, _ROOMS_KEY).get(key):
            return None
        bind_channel(guild_id, key, channel.id)
    except (RuntimeError, OSError) as e:
        log.warning(f"could not bind {key}: {e!r}")
        return None
    return key


def _table(guild_id, field):
    got = settings.get(guild_id, field, {})
    return got if isinstance(got, dict) else {}


def _bind(guild_id, field, key, target_id):
    table = _table(guild_id, field)
    if target_id is None:
        table.pop(key, None)
    else:
        table[key] = int(target_id)
    settings.put(guild_id, **{field: table})


# ---------- reading ----------

def channel(guild, key):
    """The channel bound to this job, or None if it is unbound or gone.

    A channel that has been deleted reads as unbound rather than as an error,
    so a server that tidies up does not break Eugene; `prune` clears the
    stale id the next time anything checks.
    """
    if guild is None:
        return None
    cid = _table(guild.id, _ROOMS_KEY).get(key)
    return guild.get_channel(cid) if cid else None


def category(guild, key):
    if guild is None:
        return None
    cid = _table(guild.id, _CATS_KEY).get(key)
    got = guild.get_channel(cid) if cid else None
    # A category is the thing that holds channels; if the id now points at
    # something else entirely, treat it as unbound rather than hand back a
    # text channel to something expecting a category.
    return got if got is not None and hasattr(got, "channels") else None


def role(guild, key):
    if guild is None:
        return None
    rid = _table(guild.id, _ROLES_KEY).get(key)
    return guild.get_role(rid) if rid else None


def bound_channel_id(guild_id, key):
    return _table(guild_id, _ROOMS_KEY).get(key)


def bound_room_ids(guild_id):
    """Every channel this server has pointed at a job, whatever the job.

    The set of rooms that are his, for the one question that does not care
    which room is which: is he in one of his own rooms, or in somebody
    else's.
    """
    return {int(v) for v in _table(guild_id, _ROOMS_KEY).values() if v}


# ---------- writing ----------

def bind_channel(guild_id, key, channel_id):
    _bind(guild_id, _ROOMS_KEY, key, channel_id)


def bind_category(guild_id, key, category_id):
    _bind(guild_id, _CATS_KEY, key, category_id)


def bind_role(guild_id, key, role_id):
    _bind(guild_id, _ROLES_KEY, key, role_id)


# ---------- health ----------

def missing_rooms(guild):
    return [k for k in ROOMS if channel(guild, k) is None]


def missing_roles(guild):
    return [k for k in ROLES if role(guild, k) is None]


def ready(guild):
    """Whether governance can run: the rooms it cannot do without, plus the
    role that decides who votes."""
    return (
        all(channel(guild, k) is not None for k in ESSENTIAL_ROOMS)
        and role(guild, "cooperative") is not None
    )


def prune(guild):
    """Drop bindings that no longer point at anything, or no longer point
    at anything he does. Returns what it dropped, so an admin can be told
    rather than left wondering why a room went quiet.

    Two kinds of dead. A channel somebody deleted is the obvious one. The
    other is a job that stopped existing: a server set up before a feature
    was removed keeps a binding for it for ever, because nothing ever
    looked at the key. That is not only untidy -- `bound_room_ids` is the
    set of rooms he treats as his own, and it reads every value in the
    table whatever the key, so four rooms belonging to features he no
    longer has were four more rooms he would answer and listen in.
    """
    if guild is None:
        return []
    dropped = []
    for field, table, lookup, known in (
        (_ROOMS_KEY, _table(guild.id, _ROOMS_KEY), guild.get_channel, ROOMS),
        (_CATS_KEY, _table(guild.id, _CATS_KEY), guild.get_channel, CATEGORIES),
        (_ROLES_KEY, _table(guild.id, _ROLES_KEY), guild.get_role, ROLES),
    ):
        for key, target_id in list(table.items()):
            if key not in known:
                _bind(guild.id, field, key, None)
                dropped.append(f"{field}.{key} (no such job any more)")
                continue
            if lookup(target_id) is None:
                _bind(guild.id, field, key, None)
                dropped.append(f"{field}.{key}")
    if dropped:
        log.warning(f"pruned dangling bindings: {', '.join(dropped)}")
    return dropped


def summary(guild, wanted=None, required=None):
    """One line per job, for the setup screen.

    `wanted` and `required` come from the modules that are switched on, so
    the screen lists the jobs this server actually has a use for and marks
    the ones something cannot run without. Called with neither, it falls
    back to every job there is and the old fixed idea of what is essential
    -- which is right for a caller that has no opinion, and wrong for a
    server that has switched governance off and does not need a votes room.
    """
    rooms = list(wanted) if wanted is not None else list(ROOMS)
    must = set(required) if required is not None else set(ESSENTIAL_ROOMS)
    lines = []
    for key, label in ROLES.items():
        got = role(guild, key)
        lines.append(f"{'✅' if got else '⬜'} **{key}**: {got.mention if got else label}")
    for key in rooms:
        got = channel(guild, key)
        optional = "" if key in must else " *(optional)*"
        lines.append(
            f"{'✅' if got else '⬜'} **{key}**{optional}: "
            f"{got.mention if got else ROOMS.get(key, '')}"
        )
    for key, label in CATEGORIES.items():
        got = category(guild, key)
        lines.append(
            f"{'✅' if got else '⬜'} **{key}** *(optional)*: "
            f"{got.name if got else label}"
        )
    return "\n".join(lines)
