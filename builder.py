"""Shaping a server into the place Eugene can keep.

1. Sweeps pre-launch channels: text channels not in the config are renamed
   archived_<name>, locked, and moved to the archive category. Voice
   channels not in the config are deleted (voice has no history). Emptied
   stray categories are removed.
2. Builds the structure it is given.

Idempotent: safe to re-run. Channels matching the config are adopted and
updated.

Two layouts feed it, from two different places, and the difference is the
whole design. `from_modules()` generates the rooms the enabled features
ask for -- that is the default install, and it is generated rather than
written down so it cannot describe a server other than the one the
switches produce. `hangout_only()` is what is left of
`server_config.yaml` once the governance half moved into `modules.py`:
memes, pets, gaming, voice. Eugene has no business deciding whether a
server has a memes channel, so that half is only ever built when somebody
asks for it by name.

This module is the work itself and knows nothing about how it was asked
for. Two callers share it: build_server.py, for a human at a terminal
with the repo checked out, and `/setup` inside Discord, for someone
installing Eugene in their own server with no terminal at all. They must
do the same thing, so they run the same code; `say` is the only
difference between a printed line and a line in a Discord reply.
"""

import copy

import discord

import bindings
import modules

COOPERATIVE = "Cooperative"  # holds a vote: the people who picked up a chore
MEMBER = "Member"            # in the room, no vote
NERD = "nerd"    # opt-in subscription to the bot-health channel, not a rank

# Roles this server used to call something else. Renamed in place rather than
# recreated, so nobody loses a role they already hold.
FORMER_NAMES = {COOPERATIVE: ("Key", "Citizen")}


def base_name(name):
    """Channel identity ignoring the emoji prefix: '🥗・food' -> 'food'.
    Lets emoji swaps rename in place instead of archive-and-recreate."""
    return name.split("・", 1)[-1]


def former_bases(spec):
    """Base names a channel used to go by, so a rename adopts the existing
    channel and keeps its history instead of starting an empty one."""
    return {base_name(n) for n in spec.get("former") or []}


def config_channels(config):
    """Yield (category_name, spec, is_voice) for every configured channel."""
    for cat in config["categories"]:
        for spec in cat.get("channels") or []:
            yield cat["name"], spec, spec.get("type") == "voice"


ARCHIVE = "🗄️ archive"


def from_modules(guild_id):
    """The layout the enabled modules add up to, in this file's own shape.

    There is one description of the governance rooms and it lives in
    `modules.py`, because there used to be two -- this file's config and
    the table `/setup` created rooms from -- and a server set up both ways
    ended up with two of everything. The terminal builder and the panel now
    read the same plan, generated from the same switches, so they cannot
    disagree about what a server needs or what a room is called.

    The archive is appended whatever is switched on: it is not a feature,
    it is where a closed debate chamber goes, and `ensure_archive` wants a
    category to put things in either way.
    """
    categories = []
    for name, rooms in modules.structure(guild_id, only_buildable=True):
        specs = [modules.ROOM_PLAN[job] for job in rooms]
        categories.append({
            "name": name,
            "governance": True,
            "visibility": ("cooperative" if name == "governance" else "members"),
            "channels": [
                {
                    "name": spec["name"],
                    "topic": spec["topic"],
                    "visibility": spec["visibility"],
                    "read_only": spec["read_only"],
                }
                for spec in specs
            ],
        })
    categories.append({
        "name": ARCHIVE, "governance": True, "visibility": "owner",
        "channels": [],
    })
    return {"categories": categories}


def hangout_only(config):
    """The parts of `server_config.yaml` that are nobody's business but the
    server's: the hangout, the voice rooms, the front door. The governance
    half of that file moved into `modules.py`; what is left is a starting
    point for an empty server and is only ever built when somebody asks for
    it by name."""
    wide = copy.deepcopy(config)
    wide["categories"] = [
        c for c in config["categories"] if not c.get("governance")
    ]
    return wide


def merge(*configs):
    """One layout out of several, in the order given. Categories with the
    same name are not merged -- there are none, and silently combining two
    that happened to collide would be a surprise rather than a feature."""
    out = {"categories": []}
    seen = set()
    for config in configs:
        for cat in config["categories"]:
            if cat["name"] in seen:
                continue
            seen.add(cat["name"])
            out["categories"].append(copy.deepcopy(cat))
    return out


async def ensure_role(guild, name, reason, say):
    role = discord.utils.get(guild.roles, name=name)
    if role is not None:
        return role
    # Adopt a previous name before creating anything: a fresh role would
    # leave everyone who holds the old one outside the new one.
    for old in FORMER_NAMES.get(name, ()):
        stale = discord.utils.get(guild.roles, name=old)
        if stale is not None:
            await stale.edit(name=name, reason=f"Renamed from {old}")
            say(f"renamed role: {old} -> {name}")
            return stale
    role = await guild.create_role(
        name=name, permissions=discord.Permissions.none(), reason=reason
    )
    say(f"created role: {name}")
    return role


async def ensure_cooperative(guild, say):
    return await ensure_role(
        guild, COOPERATIVE, "Holds a vote; the boundary between door and room", say
    )


async def ensure_member(guild, say):
    return await ensure_role(
        guild, MEMBER, "In the room, no vote", say
    )


def overwrites_for(guild, cooperative, owner, spec, default_vis="members",
                   nerd=None, member=None):
    """Build overwrites from visibility + read_only.

    Members and the cooperative see exactly the same rooms. The difference
    between them is what the buttons let them do, never what they are allowed
    to know: everything except an individual ballot is public to everyone in.
    """
    vis = spec.get("visibility", default_vis)
    # Both roles, wherever one of them is named. `member` is optional so a
    # caller that predates the split still builds a working server.
    inside = [r for r in (cooperative, member) if r is not None]
    no_posting = dict(
        send_messages=False,
        add_reactions=True,
        create_public_threads=False,
        create_private_threads=False,
        send_messages_in_threads=False,
    )
    seen = discord.PermissionOverwrite(view_channel=True)
    hidden = discord.PermissionOverwrite(view_channel=False)
    if vis == "gate":
        everyone_ow = (
            discord.PermissionOverwrite(view_channel=True, **no_posting)
            if spec.get("read_only")
            else discord.PermissionOverwrite(view_channel=True)
        )
        ow = {guild.default_role: everyone_ow}
        ow.update({r: seen for r in inside})
    elif vis == "members":
        ow = {guild.default_role: hidden}
        ow.update({r: seen for r in inside})
        if spec.get("read_only"):
            quiet = discord.PermissionOverwrite(view_channel=True, **no_posting)
            ow.update({r: quiet for r in inside})
            ow[guild.default_role] = discord.PermissionOverwrite(
                view_channel=False, **no_posting
            )
    elif vis == "cooperative":
        # Where the cooperative proposes, argues and votes. Members are
        # outside this deliberately; what comes out of it is published to
        # everyone in #decisions, which is the part that stays open.
        ow = {guild.default_role: hidden}
        if member is not None:
            ow[member] = hidden
        ow[cooperative] = (
            discord.PermissionOverwrite(view_channel=True, **no_posting)
            if spec.get("read_only")
            else seen
        )
    elif vis == "owner":
        ow = {guild.default_role: hidden}
        ow.update({r: hidden for r in inside})
        ow[owner] = discord.PermissionOverwrite(view_channel=True, **no_posting)
    elif vis == "nerds":
        if nerd is None:
            raise ValueError("nerds visibility requires the nerd role")
        nerd_ow = (
            discord.PermissionOverwrite(view_channel=True, **no_posting)
            if spec.get("read_only")
            else discord.PermissionOverwrite(view_channel=True)
        )
        ow = {guild.default_role: hidden}
        ow.update({r: hidden for r in inside})
        ow[nerd] = nerd_ow
    else:
        raise ValueError(f"unknown visibility {vis!r}")
    return ow


def archive_category_name(config):
    for cat in config["categories"]:
        if "archive" in cat["name"].lower():
            return cat["name"]
    return ARCHIVE


async def ensure_archive(guild, config, cooperative, owner, say, member=None):
    name = archive_category_name(config)
    ow = overwrites_for(guild, cooperative, owner, {"visibility": "owner"},
                        member=member)
    cat = discord.utils.get(guild.categories, name=name)
    if cat is None:
        cat = await guild.create_category(name, overwrites=ow)
        say(f"created category: {name}")
    else:
        await cat.edit(overwrites=ow)
    return cat


async def sweep(guild, config, cooperative, owner, say, member=None):
    """Archive text channels, delete voice channels, remove stray categories."""
    keep_text = {s["name"] for _, s, v in config_channels(config) if not v}
    keep_voice = {s["name"] for _, s, v in config_channels(config) if v}
    keep_cats = {c["name"] for c in config["categories"] if c["name"]}

    archive = await ensure_archive(guild, config, cooperative, owner, say, member)
    hidden = overwrites_for(guild, cooperative, owner, {"visibility": "owner"},
                            member=member)

    def is_chamber(ch):
        # live debate chambers (🗳️ bill categories) belong to the clerk,
        # not the builder; never sweep them
        return ch.category is not None and ch.category.name.startswith("🗳️")

    keep_bases = {base_name(n) for n in keep_text}
    for _cat, _spec, _voice in config_channels(config):
        if not _voice:
            keep_bases |= former_bases(_spec)
    for ch in list(guild.text_channels):
        if ch.name in keep_text or is_chamber(ch):
            continue
        if not ch.name.startswith("archived_") and base_name(ch.name) in keep_bases:
            continue  # emoji rename pending; build() adopts it by base name
        if ch.category == archive:
            await ch.edit(overwrites=hidden)  # re-stamp: self-heal any drift
            continue
        new_name = ch.name if ch.name.startswith("archived_") else f"archived_{ch.name}"
        await ch.edit(
            name=new_name,
            category=archive,
            sync_permissions=False,
            overwrites=hidden,
        )
        say(f"archived: #{ch.name} -> #{new_name}")

    for vc in list(guild.voice_channels):
        if vc.name in keep_voice or is_chamber(vc):
            continue
        await vc.delete(reason="Pre-launch sweep: voice has no history")
        say(f"deleted voice: {vc.name}")

    for cat in list(guild.categories):
        if cat.name not in keep_cats and not cat.channels:
            await cat.delete(reason="Pre-launch sweep: emptied category")
            say(f"deleted category: {cat.name}")


async def build(guild, config, cooperative, owner, nerd, say, member=None):
    """Create or adopt everything in server_config.yaml, in order."""
    position = 0
    for cat_spec in config["categories"]:
        category = None
        cat_vis = cat_spec.get("visibility", "members")
        if cat_spec["name"]:
            cat_ow = overwrites_for(guild, cooperative, owner,
                                    {"visibility": cat_vis}, nerd=nerd, member=member)
            category = discord.utils.get(guild.categories, name=cat_spec["name"])
            if category is None:
                category = await guild.create_category(cat_spec["name"], overwrites=cat_ow)
                say(f"created category: {cat_spec['name']}")
            else:
                await category.edit(overwrites=cat_ow)
            await category.edit(position=position)
            position += 1

        for spec in cat_spec.get("channels") or []:
            is_voice = spec.get("type") == "voice"
            overwrites = overwrites_for(
                guild, cooperative, owner, spec, default_vis=cat_vis,
                nerd=nerd, member=member,
            )

            if is_voice:
                existing = discord.utils.get(guild.voice_channels, name=spec["name"])
                if existing is None:
                    await guild.create_voice_channel(
                        spec["name"], category=category,
                        user_limit=spec.get("user_limit", 0), overwrites=overwrites,
                    )
                    say(f"created voice: {spec['name']}")
                else:
                    await existing.edit(
                        category=category,
                        user_limit=spec.get("user_limit", 0),
                        overwrites=overwrites,
                    )
                    say(f"adopted voice: {spec['name']}")
            else:
                existing = discord.utils.get(guild.text_channels, name=spec["name"])
                if existing is None:
                    wanted = {base_name(spec["name"])} | former_bases(spec)
                    existing = next(
                        (
                            c
                            for c in guild.text_channels
                            if not c.name.startswith("archived_")
                            and base_name(c.name) in wanted
                        ),
                        None,
                    )
                if existing is None:
                    existing = await guild.create_text_channel(
                        spec["name"], category=category,
                        topic=spec.get("topic"), overwrites=overwrites,
                    )
                    say(f"created text: #{spec['name']}")
                else:
                    old_name = existing.name
                    await existing.edit(
                        name=spec["name"], category=category,
                        topic=spec.get("topic"), overwrites=overwrites,
                    )
                    say(
                        f"renamed text: #{old_name} -> #{spec['name']}"
                        if old_name != spec["name"]
                        else f"adopted text: #{spec['name']}"
                    )
                # Building a room and telling Eugene which room it is are two
                # different things, and this used to do only the first. A
                # server built from the terminal arrived with every governance
                # channel present and none of them bound, so the first person
                # to set up from inside Discord afterwards got a second set. It
                # binds nothing anybody has already bound by hand, and a host
                # with no settings store yet simply gets nothing.
                took = bindings.adopt(guild.id, existing)
                if took:
                    say(f"bound `{took}` → #{existing.name}")


async def enforce_order(guild, config):
    """Make sidebar order match config order exactly, per category and
    per bucket (Discord lists text channels first, then voice)."""
    for cat_spec in config["categories"]:
        category = (
            discord.utils.get(guild.categories, name=cat_spec["name"])
            if cat_spec["name"]
            else None
        )
        text_i = voice_i = 0
        for spec in cat_spec.get("channels") or []:
            is_voice = spec.get("type") == "voice"
            pool = guild.voice_channels if is_voice else guild.text_channels
            channel = discord.utils.get(pool, name=spec["name"])
            if channel is None:
                continue
            kwargs = {"beginning": True, "offset": voice_i if is_voice else text_i}
            if category:
                kwargs["category"] = category
            await channel.move(**kwargs)
            if is_voice:
                voice_i += 1
            else:
                text_i += 1


def missing_permissions(guild):
    """What Eugene cannot do here. Checked before touching anything:
    a build that dies halfway leaves a server in a worse state than one
    that never started."""
    me = guild.me
    if me is None:
        return ["Eugene is not a member of this server"]
    lacking = []
    if not me.guild_permissions.administrator:
        lacking.append("Administrator")
    if not me.guild_permissions.manage_channels:
        lacking.append("Manage Channels")
    if not me.guild_permissions.manage_roles:
        lacking.append("Manage Roles")
    return lacking


async def install(guild, config, say=print, sweep_existing=False):
    """Shape a guild into the room. Idempotent; safe to re-run.

    `config` is whatever the caller wants built -- pass `governance_only()`
    of it to leave the server's own rooms alone.

    `sweep_existing` archives every text channel not in the config and
    DELETES every voice channel not in it. It only makes sense on a fresh
    server whose config you wrote yourself, so it is off unless a caller
    asks for it in as many words. Adopting and creating are always safe;
    sweeping is not, and defaulting it on has no upside. Sweeping against
    a filtered config would archive everything the filter dropped, which
    is the whole server; callers must not do that, and build_server.py
    refuses the combination."""
    cooperative = await ensure_cooperative(guild, say)
    member = await ensure_member(guild, say)
    nerd = await ensure_role(
        guild, NERD, "Opt-in bot-health visibility, not a rank", say
    )
    owner = guild.owner or await guild.fetch_member(guild.owner_id)
    if sweep_existing:
        say("-- sweep (destructive) --")
        await sweep(guild, config, cooperative, owner, say, member)
    else:
        say("-- sweep skipped: nothing existing will be archived or deleted --")
        await ensure_archive(guild, config, cooperative, owner, say, member)
    say("-- build --")
    await build(guild, config, cooperative, owner, nerd, say, member)
    await enforce_order(guild, config)
    say("-- done --")
