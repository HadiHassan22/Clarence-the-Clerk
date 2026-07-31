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
    "polls": "Polls open to everyone, members included",
    "health": "Eugene's own vitals",
    "welcome": "Where new arrivals land, and where invite links point",
    "chat": "The only room Eugene talks in (unset: anywhere)",
}

# Rooms without which governance cannot run at all.
ESSENTIAL_ROOMS = ("votes", "decisions")

ROLES = {
    "cooperative": "Holds a vote",
    "member": "In the room, without a vote",
}

# Categories Eugene files things under, rather than posts into.
CATEGORIES = {
    "chambers": "Where a live debate chamber is created",
    "archive": "Where a closed debate chamber is filed",
}

_ROOMS_KEY = "rooms"
_ROLES_KEY = "roles"
_CATS_KEY = "categories"


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
    """Drop bindings whose target no longer exists. Returns what it dropped,
    so an admin can be told rather than left wondering why a room went quiet."""
    if guild is None:
        return []
    dropped = []
    for field, table, lookup in (
        (_ROOMS_KEY, _table(guild.id, _ROOMS_KEY), guild.get_channel),
        (_CATS_KEY, _table(guild.id, _CATS_KEY), guild.get_channel),
        (_ROLES_KEY, _table(guild.id, _ROLES_KEY), guild.get_role),
    ):
        for key, target_id in list(table.items()):
            if lookup(target_id) is None:
                _bind(guild.id, field, key, None)
                dropped.append(f"{field}.{key}")
    if dropped:
        log.warning(f"pruned dangling bindings: {', '.join(dropped)}")
    return dropped


def summary(guild):
    """One line per job, for the setup screen."""
    lines = []
    for key, label in ROLES.items():
        got = role(guild, key)
        lines.append(f"{'✅' if got else '⬜'} **{key}** — {got.mention if got else label}")
    for key, label in ROOMS.items():
        got = channel(guild, key)
        essential = "" if key in ESSENTIAL_ROOMS else " *(optional)*"
        lines.append(
            f"{'✅' if got else '⬜'} **{key}**{essential} — "
            f"{got.mention if got else label}"
        )
    for key, label in CATEGORIES.items():
        got = category(guild, key)
        lines.append(
            f"{'✅' if got else '⬜'} **{key}** *(optional)* — "
            f"{got.name if got else label}"
        )
    return "\n".join(lines)
