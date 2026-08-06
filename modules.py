"""What Eugene does here, in ten parts, each one switchable.

Everything the clerk does used to be either always on or gated by a
setting buried in `warden.SPEC` next to forty others. That made two
questions unanswerable from inside Discord: *what is he doing in my
server*, and *how do I stop him doing one of them*. A server that wanted
the parliament and none of the moderation had to know that `automod` is a
setting group and `memory` is not; a server that wanted the moderation and
none of the parliament had no answer at all.

So the features are named. A module is one thing a member would say the
bot does -- "it runs our votes", "it greets people" -- with everything
that thing needs declared in one place: the rooms it posts in, the roles
it reads, the settings it owns, the tools it lends the model, and the
other modules it stands on. That declaration is doing five jobs at once:

- the setup panel lists modules rather than settings, and can say why one
  is not working yet;
- the structure preview is generated from the modules that are on, so
  what the server will look like is knowable before anything is built;
- the builder makes exactly the rooms the enabled modules asked for, and
  no others -- which is the promise that Eugene never turns up in
  somebody's server and creates a memes channel;
- the model's tool list shrinks to the modules that are on, so a switched
  off feature cannot be talked into existence;
- `/house` groups its settings by the feature they belong to.

Discord-free and free of `clerk.py`, like `warden.py`, so
every rule in here is testable on a laptop. Where a question needs to know
what a guild actually has -- is the votes room bound, is there a key --
the caller passes in what it found; this module decides what that means.

Two rules hold the whole thing together:

**Off is off, and dormant is not off.** A module the house switched off
does nothing and says so. A module that is on but missing something it
needs is *dormant*: it does nothing either, but the reason is a gap
rather than a decision, and the panel names the gap. Conflating the two
is how a bot ends up silently not doing the thing you asked for.

**A dependency is not a suggestion.** A module that names another
because both are things he does with a brain he may not have. Switching
`chat` off switches them off in fact, whatever their own flag says, and
`enabled()` is the only function that gets to answer that question.
"""

from __future__ import annotations

import logging

import settings

log = logging.getLogger("modules")

# One settings key holds every switch, for the same reason `warden.KEY`
# does: a server's choices read as one object, and a module nobody has
# touched keeps following the default instead of being frozen at whatever
# it was the day they changed something else.
KEY = "modules"


# ---------- the rooms modules ask for ----------
#
# The only description of the governance layout there is. `server_config.yaml`
# used to hold a second one, and the two of them disagreeing about what the
# votes room is called is what put two of every governance channel into the
# first server that was set up both ways.
#
# `name` is a suggestion and nothing more: a channel that already exists is
# adopted exactly as it stands, never renamed, never re-topiced, never moved.
# `category` groups them in the preview and in the build.

# One category, and one only. There used to be two -- governance for the
# cooperative's rooms and commons for the open ones -- which meant a server
# that installed him got two categories it had not asked for, filed by a
# distinction only this file cares about. Who may read a room is already
# written on the room, in `visibility`, and that is where it belongs: a
# second category said the same thing again in the sidebar and got in the
# way of every server that already had its own idea of how to file things.
CATEGORIES = {
    "governance": "Eugene's rooms: the cooperative's, the server's record, "
                  "and the one he talks in.",
}

# `visibility` is the builder's vocabulary, so this table can drive both
# routes into a server without a translation step between them:
#   gate        - visible to everyone, role or no role
#   members     - visible to anyone inside
#   cooperative - the cooperative only
#   admins      - administrators and the owner, and nobody else. There is no
#                 role for this and there must not be one: Discord's
#                 Administrator permission bypasses channel overwrites on
#                 its own, so denying everybody leaves exactly the people
#                 who could already read it by hand. A named role would be
#                 a gate whose keys can be copied by the people it gates.
ROOM_PLAN = {
    "proposals": {
        "name": "propose", "category": "governance",
        "visibility": "cooperative", "read_only": True,
        "topic": "Say what should change, and why. Eugene files it and the "
                 "cooperative votes.",
    },
    "votes": {
        "name": "votes", "category": "governance",
        "visibility": "cooperative", "read_only": True,
        # Only Eugene posts in the room itself; the argument about a
        # proposal happens in the thread he opens on it, and a thread
        # nobody may write in is not a debate. This opens that and nothing
        # else -- the room stays his.
        "threads": True,
        "topic": "Proposals up for a vote, and nothing else. Argue in the "
                 "thread, ballots by button, always anonymous. Results are "
                 "in #decisions.",
    },
    "decisions": {
        "name": "decisions", "category": "governance",
        "visibility": "members", "read_only": True,
        "topic": "Every vote that has closed, one message each. Decisions "
                 "numbered, and the window to take one back.",
    },
    "polls": {
        "name": "community-polls", "category": "governance",
        # Everyone, role or no role. This is the only room he keeps that a
        # person with nothing at all can answer in, and that is the whole
        # point of it: a poll the cooperative could reach and nobody else
        # could is a proposal with extra steps.
        "visibility": "gate", "read_only": True,
        "topic": "Questions put to everyone here. Anyone can answer, "
                 "answers are anonymous, and nothing decided in this room "
                 "binds anybody: what the cooperative decides is in "
                 "#decisions.",
    },
    "chat": {
        # His own room, made under his own name. A server with a channel
        # called `chat` has not thereby asked to be confined to it, and the
        # old plan -- adopt nothing, build nothing, talk everywhere until
        # somebody points you at a room -- meant the ordinary install had
        # him answering in every channel in the building. So he makes the
        # room, and the name says whose it is: nothing that already exists
        # is called `eugene-chat`, so nothing that already exists is taken
        # over by it.
        "name": "eugene-chat", "category": "governance",
        "visibility": "members", "read_only": False,
        "topic": "Talk to Eugene here. Mention him.",
        "adopt": False,
    },
}


# ---------- the modules ----------
#
# rooms:    binding key -> True if the module cannot run without it.
#           An optional room is one the module improves with and survives
#           without: `chat` unbound means he talks everywhere, not nowhere.
# roles:    binding key -> True if the module cannot run without it, read
#           exactly like the rooms above. It was a bare tuple while every
#           role in it was required, and the bell is not: a house that
#           never made one holds votes perfectly well, so a required entry
#           would put governance into dormant on every server that upgraded
#           and tell them a room was missing that nobody asked for.
# needs:    other modules. Off downwards, always.
# brain:    True if it cannot do anything without an AI key.
# settings: the `warden.SPEC` groups this module owns, so `/house` can
#           group them and a switched-off module's settings can be marked
#           inert rather than looking live. Those groups say how a feature
#           behaves once it is on, never whether it is on: `warden.SPEC`
#           used to carry `automod.enabled` and `welcome.enabled` beside
#           beside these switches, which meant a server could tick Levels
#           here, watch it go green, and get nothing. One question, one
#           switch, and this is the switch.
# tools:    what it lends the model. A tool belonging to a module that is
#           off is not declared and would be refused if it were.
# commands: the slash commands it answers, for saying what went quiet.
# builds:   whether `/setup` offers to create its rooms. A module that
#           only ever adopts (chat) says False, so nobody gets a channel
#           they did not ask for.
#
# Every default is what the clerk already did before there were modules, so
# an upgrade in place changes nothing and every switch below is a real
# switch rather than a quiet change of behaviour. That is why `moderation`
# starts off -- its filters were off by default already -- and why `chat` starts on even though a
# server with no AI key sees nothing from any of them. On with no key is
# dormant, and dormant says so.

SPEC = {
    "governance": {
        "name": "Governance",
        "blurb": "Proposals, anonymous ballots, and the permanent record.",
        "default": True,
        "rooms": {"votes": True, "decisions": True, "proposals": False},
        # `bell` is who asked to be told a ballot has opened. Optional
        # because nobody has to be told: the floor is a room you can go and
        # look at, and a house where nobody wants ringing should look
        # exactly like a house that never heard of the bell.
        "roles": {"cooperative": True, "bell": False},
        "needs": (),
        "brain": False,
        "settings": (),
        "builds": True,
        "commands": ("propose", "invite", "remove", "close", "bills"),
        "tools": ("propose", "close_floor", "mark_carried_out",
                  "set_nudges", "lookup"),
    },
    "polls": {
        "name": "Community polls",
        "blurb": "A question put to the whole server, answered anonymously "
                 "and deciding nothing.",
        # Off, and this is the one switch in the table where the default is
        # doing more work than a default usually does. A poll is the only
        # thing Eugene runs that reaches past the cooperative, and the
        # house that has not asked for that should not have a feature
        # sitting there that a conversation could wander into. Off means
        # the tool is not declared, so it is not a thing he can be talked
        # into and not a thing he can offer: a server that never switches
        # this on has a clerk who has never heard of a poll.
        "default": False,
        "rooms": {"polls": True},
        "roles": {},
        "needs": (),
        "brain": False,
        "settings": (),
        "builds": True,
        "commands": ("poll",),
        "tools": ("open_community_poll",),
    },
    "chat": {
        "name": "Conversation",
        "blurb": "He answers when mentioned, and does as he is asked. "
                 "Needs an AI key, and is the only part of him that sends "
                 "anything outside your server.",
        # Off. It used to be on and dormant, so a fresh install with no key
        # showed a feature waiting on something and read as half-broken --
        # when in fact the whole governance engine was running and nothing
        # was missing. A server that wants him to talk switches it on and
        # sets a key, which is one decision made on purpose rather than a
        # default nobody chose.
        "default": False,
        "rooms": {"chat": False},
        "roles": {},
        "needs": (),
        "brain": True,
        "settings": (),
        # He builds it now. This was False while the plan was that
        # conversation lived in a room the server already had, which in
        # practice meant no room and every room: unbound, he answered
        # wherever he was mentioned. One room he makes himself is the
        # smaller promise and the easier one to keep.
        "builds": True,
        "commands": (),
        "tools": ("server_info",),
    },
}

# Display order for every list a human reads: what he is for, then what he
# can be talked into, then the housekeeping. Not alphabetical -- the first
# three are the reason to install him and belong at the top.
ORDER = ("governance", "polls", "chat")

# Tools that belong to no module because they are how a module is
# configured. Switching every feature off must still leave the way to
# switch one back on.
UNGATED_TOOLS = ("list_settings", "set_setting", "reset_settings",
                 "set_feature", "list_features")


def keys():
    """Every module, in the order a human should read them."""
    return [k for k in ORDER if k in SPEC] + [k for k in SPEC if k not in ORDER]


def spec(key):
    return SPEC.get(key)


def name(key):
    got = SPEC.get(key)
    return got["name"] if got else key


# ---------- reading and writing the switches ----------

def _table(guild_id) -> dict:
    got = settings.get(guild_id, KEY) or {}
    return got if isinstance(got, dict) else {}


def chosen(guild_id) -> dict:
    """Only what this house has actually decided, for telling them which
    switches are theirs and which are simply the ones he came with."""
    return {k: bool(v) for k, v in _table(guild_id).items() if k in SPEC}


def switched_on(guild_id, key) -> bool:
    """This module's own switch, ignoring its dependencies. The panel needs
    this to show a module as blocked rather than as off: somebody who turned
    `memory` on and then turned `chat` off should see why it went quiet, not
    a switch that appears to have flipped itself."""
    if key not in SPEC:
        return False
    got = _table(guild_id).get(key)
    return bool(got) if isinstance(got, bool) else SPEC[key]["default"]


def enabled(guild_id, key) -> bool:
    """Whether this module runs at all. The only answer to that question.

    Dependencies are followed here rather than at every call site, because
    a call site that forgets is a feature that keeps running after the
    house switched off the thing it stands on.
    """
    if not switched_on(guild_id, key):
        return False
    return all(enabled(guild_id, dep) for dep in SPEC[key]["needs"])


def dependants(key):
    """Modules that stand on this one, directly or through another."""
    found = set()
    frontier = [key]
    while frontier:
        current = frontier.pop()
        for other, entry in SPEC.items():
            if current in entry["needs"] and other not in found:
                found.add(other)
                frontier.append(other)
    return [k for k in keys() if k in found]


def set_enabled(guild_id, key, on):
    """Flip one switch. Returns (changed, knock_on).

    `knock_on` names the modules this drags with it, so the reply can say
    so rather than leaving somebody to notice next week: switching `chat`
    off takes its dependants with it in fact, and switching one of
    those on is meaningless while `chat` is off, so it turns `chat` on too.
    """
    if key not in SPEC:
        return False, []
    on = bool(on)
    was = enabled(guild_id, key)
    table = dict(_table(guild_id))
    knock_on = []
    if on:
        for dep in SPEC[key]["needs"]:
            if not switched_on(guild_id, dep):
                table[dep] = True
                knock_on.append(dep)
    else:
        knock_on = [k for k in dependants(key) if enabled(guild_id, k)]
    table[key] = on
    # A switch set back to the default stops being stored, so a house that
    # changes its mind is not left holding a copy of a value it never chose
    # -- and a later change to a default reaches them.
    for k in list(table):
        if k not in SPEC or table[k] == SPEC[k]["default"]:
            table.pop(k, None)
    settings.put(guild_id, **{KEY: table or None})
    return enabled(guild_id, key) != was, knock_on


def apply_set(guild_id, wanted):
    """Set the whole roll at once: every module in `wanted` on, the rest
    off. What the modules menu submits, since a multi-select hands back the
    complete selection rather than the one thing that changed.

    Returns (turned_on, turned_off) by module key.
    """
    wanted = {k for k in wanted if k in SPEC}
    # Dependencies first, so a selection of {memory} without {chat} comes
    # out as both on rather than as one silently blocked module.
    for key in list(wanted):
        wanted |= set(_ancestors(key))
    before = {k: enabled(guild_id, k) for k in SPEC}
    table = {k: (k in wanted) for k in SPEC
             if (k in wanted) != SPEC[k]["default"]}
    settings.put(guild_id, **{KEY: table or None})
    after = {k: enabled(guild_id, k) for k in SPEC}
    return (
        [k for k in keys() if after[k] and not before[k]],
        [k for k in keys() if before[k] and not after[k]],
    )


def _ancestors(key):
    out = []
    for dep in SPEC[key]["needs"]:
        out.append(dep)
        out.extend(_ancestors(dep))
    return out


def reset(guild_id):
    settings.put(guild_id, **{KEY: None})


# ---------- what a module is still waiting for ----------
#
# The caller says what the guild has; this decides what that means. Keeping
# the Discord lookups on the other side of the wall is what makes every
# rule below testable without a server, and it is the same split
# `warden.py` uses for channels and roles.

def blockers(guild_id, key, *, rooms=(), roles=(), brain=False):
    """Why an enabled module is not actually working. Empty means it is.

    `rooms` and `roles` are the binding keys that currently resolve to
    something real; `brain` is whether this server has an AI key.
    """
    entry = SPEC.get(key)
    if entry is None:
        return ["there is no such module"]
    missing = []
    for dep in entry["needs"]:
        if not enabled(guild_id, dep):
            missing.append(f"{name(dep).lower()} is switched off")
    for room, required in entry["rooms"].items():
        if required and room not in set(rooms):
            missing.append(f"no `{room}` room is bound")
    for role, required in entry["roles"].items():
        if required and role not in set(roles):
            missing.append(f"no `{role}` role is bound")
    if entry["brain"] and not brain:
        missing.append("no AI key")
    return missing


def status(guild_id, key, *, rooms=(), roles=(), brain=False):
    """One word for the panel: off, blocked, dormant, or on.

    `blocked` and `dormant` both mean nothing is happening, and they are
    separate because the fix is different: one is a switch somebody flipped
    and the other is a gap nobody has filled.
    """
    if not switched_on(guild_id, key):
        return "off"
    if not enabled(guild_id, key):
        return "blocked"
    return "dormant" if blockers(
        guild_id, key, rooms=rooms, roles=roles, brain=brain
    ) else "on"


def live(guild_id, key, *, rooms=(), roles=(), brain=False):
    """Enabled *and* holding everything it needs. What a feature checks at
    the top of the function that does the work."""
    return enabled(guild_id, key) and not blockers(
        guild_id, key, rooms=rooms, roles=roles, brain=brain
    )


# ---------- the structure that comes out ----------

def bound_only(room):
    """Whether this job may only ever be resolved by its binding.

    The name fallback everywhere else is deliberate and load-bearing: it is
    what lets a server work before anybody has run setup. It is also a
    guess, and for one room a wrong guess is worse than silence.
    """
    return bool(ROOM_PLAN.get(room, {}).get("bound_only"))


def adoptable_rooms(guild_id):
    """Rooms an enabled module would use if the server already has one, but
    would never create. Bound by Apply when a channel of that name is
    already there, and otherwise left alone entirely."""
    return [r for r in wanted_rooms(guild_id)
            if ROOM_PLAN.get(r, {}).get("adopt")]


def wanted_rooms(guild_id, *, only_buildable=False):
    """Every room job the enabled modules ask for, in build order.

    `only_buildable` drops the ones no module wants created -- the arrivals
    room, which is a room you already have and point him at, never one he
    makes for you.
    """
    out = []
    for key in keys():
        if not enabled(guild_id, key):
            continue
        if only_buildable and not SPEC[key]["builds"]:
            continue
        for room in SPEC[key]["rooms"]:
            if room not in out and room in ROOM_PLAN:
                out.append(room)
    order = list(ROOM_PLAN)
    return sorted(out, key=order.index)


def required_rooms(guild_id):
    """The rooms an enabled module cannot run without, so the panel can
    tell the difference between a gap and a preference."""
    out = []
    for key in keys():
        if not enabled(guild_id, key):
            continue
        for room, required in SPEC[key]["rooms"].items():
            if required and room not in out:
                out.append(room)
    return out


def wanted_roles(guild_id):
    """Every role the enabled modules ask for, needed or not. What the
    builder makes, since a role that is only ever optional still has to be
    made by somebody before anyone can hold it."""
    out = []
    for key in keys():
        if not enabled(guild_id, key):
            continue
        out.extend(r for r in SPEC[key]["roles"] if r not in out)
    return out


def required_roles(guild_id):
    """The ones an enabled module cannot run without, so the panel can tell
    the difference between a gap and a preference."""
    out = []
    for key in keys():
        if not enabled(guild_id, key):
            continue
        for role, required in SPEC[key]["roles"].items():
            if required and role not in out:
                out.append(role)
    return out


def wanted_by(guild_id, room):
    """Which enabled modules want this room. What the preview prints
    beside each line, so nobody has to guess why a channel is on the list."""
    return [k for k in keys()
            if enabled(guild_id, k) and room in SPEC[k]["rooms"]]


def structure(guild_id, *, only_buildable=False):
    """The layout the enabled modules add up to: [(category, [room, ...])].

    This is the answer to "what will my server look like". It is generated
    rather than written down, so it cannot describe a server other than the
    one the switches actually produce.
    """
    rooms = wanted_rooms(guild_id, only_buildable=only_buildable)
    out = []
    for category in CATEGORIES:
        inside = [r for r in rooms if ROOM_PLAN[r]["category"] == category]
        if inside:
            out.append((category, inside))
    loose = [r for r in rooms if ROOM_PLAN[r]["category"] not in CATEGORIES]
    if loose:
        out.append(("", loose))
    return out


# ---------- what belongs to what ----------

def of_setting(setting_key):
    """The module that owns a `warden.SPEC` key, or None. `automod.links`
    belongs to moderation; `mod.purge_max` does too."""
    group = str(setting_key).split(".", 1)[0]
    for key in keys():
        if group in SPEC[key]["settings"]:
            return key
    return None


def of_tool(tool_name):
    for key in keys():
        if tool_name in SPEC[key]["tools"]:
            return key
    return None


def tool_allowed(guild_id, tool_name):
    """Whether the model may be handed this tool here. A tool nobody
    claimed is allowed: the settings tools belong to no feature on purpose,
    and an unrecognised name is refused later by the registry anyway,
    where the error message is better."""
    owner = of_tool(tool_name)
    return owner is None or enabled(guild_id, owner)


def settings_groups(guild_id, on_only=True):
    """Warden groups to show, by module. Groups belonging to a switched-off
    module are worth hiding rather than showing as live settings nothing
    reads."""
    out = {}
    for key in keys():
        if on_only and not enabled(guild_id, key):
            continue
        for group in SPEC[key]["settings"]:
            out[group] = key
    return out


def summary(guild_id, *, rooms=(), roles=(), brain=False):
    """One line per module, for the panel and for `/house`."""
    marks = {"on": "🟢", "dormant": "🟡", "blocked": "🟠", "off": "⬜"}
    lines = []
    for key in keys():
        state = status(guild_id, key, rooms=rooms, roles=roles, brain=brain)
        tail = ""
        if state in ("dormant", "blocked"):
            why = blockers(guild_id, key, rooms=rooms, roles=roles, brain=brain)
            tail = ": " + "; ".join(why)
        lines.append(f"{marks[state]} **{SPEC[key]['name']}**{tail}")
    return "\n".join(lines)


def counts(guild_id):
    on = sum(1 for k in SPEC if enabled(guild_id, k))
    return on, len(SPEC)
