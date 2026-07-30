"""Clarence the Clerk: first run.

1. Sweeps pre-launch channels: text channels not in the config are renamed
   archived_<name>, locked, and moved to the archive category. Voice
   channels not in the config are deleted (voice has no history). Emptied
   stray categories are removed.
2. Builds the full structure from server_config.yaml.
3. Posts the charter to the charter channel as embeds, one per section,
   and pins it.

Idempotent: safe to re-run. Channels matching the config are adopted and
updated, the charter is only posted if the channel is empty.

Usage: .venv/bin/python build_server.py
"""

import os
import re
from pathlib import Path

import discord
import yaml
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

CONFIG = yaml.safe_load((HERE / "server_config.yaml").read_text())
CHARTER_TEXT = (HERE / "constitution.md").read_text()

ACCENT = discord.Colour(0xE0A458)  # lamplight

intents = discord.Intents.default()
client = discord.Client(intents=intents)


def base_name(name):
    """Channel identity ignoring the emoji prefix: '🥗・food' -> 'food'.
    Lets emoji swaps rename in place instead of archive-and-recreate."""
    return name.split("・", 1)[-1]


def config_channels():
    """Yield (category_name, spec, is_voice) for every configured channel."""
    for cat in CONFIG["categories"]:
        for spec in cat.get("channels") or []:
            yield cat["name"], spec, spec.get("type") == "voice"


CITIZEN = "Key"  # the signing role is an object you receive, not a title


async def ensure_citizen(guild):
    role = discord.utils.get(guild.roles, name=CITIZEN)
    if role is None:
        role = await guild.create_role(
            name=CITIZEN, permissions=discord.Permissions.none(),
            reason="The boundary between the door and the house",
        )
        print(f"created role: {CITIZEN}")
    return role


def overwrites_for(guild, citizen, owner, spec, default_vis="citizens"):
    """Build overwrites from visibility + read_only."""
    vis = spec.get("visibility", default_vis)
    no_posting = dict(
        send_messages=False,
        add_reactions=True,
        create_public_threads=False,
        create_private_threads=False,
        send_messages_in_threads=False,
    )
    if vis == "gate":
        everyone_ow = (
            discord.PermissionOverwrite(view_channel=True, **no_posting)
            if spec.get("read_only")
            else discord.PermissionOverwrite(view_channel=True)
        )
        ow = {
            guild.default_role: everyone_ow,
            citizen: discord.PermissionOverwrite(view_channel=True),
        }
    elif vis == "citizens":
        ow = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            citizen: discord.PermissionOverwrite(view_channel=True),
        }
        if spec.get("read_only"):
            ow[citizen] = discord.PermissionOverwrite(view_channel=True, **no_posting)
            ow[guild.default_role] = discord.PermissionOverwrite(view_channel=False, **no_posting)
    elif vis == "owner":
        ow = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            citizen: discord.PermissionOverwrite(view_channel=False),
            owner: discord.PermissionOverwrite(view_channel=True, **no_posting),
        }
    else:
        raise ValueError(f"unknown visibility {vis!r}")
    return ow


def archive_category_name():
    for cat in CONFIG["categories"]:
        if "archive" in cat["name"].lower():
            return cat["name"]
    raise RuntimeError("No archive category in config")


async def ensure_archive(guild, citizen, owner):
    name = archive_category_name()
    ow = overwrites_for(guild, citizen, owner, {"visibility": "owner"})
    cat = discord.utils.get(guild.categories, name=name)
    if cat is None:
        cat = await guild.create_category(name, overwrites=ow)
        print(f"created category: {name}")
    else:
        await cat.edit(overwrites=ow)
    return cat


async def sweep(guild, citizen, owner):
    """Archive text channels, delete voice channels, remove stray categories."""
    keep_text = {s["name"] for _, s, v in config_channels() if not v}
    keep_voice = {s["name"] for _, s, v in config_channels() if v}
    keep_cats = {c["name"] for c in CONFIG["categories"] if c["name"]}

    archive = await ensure_archive(guild, citizen, owner)
    hidden = overwrites_for(guild, citizen, owner, {"visibility": "owner"})

    keep_bases = {base_name(n) for n in keep_text}
    for ch in list(guild.text_channels):
        if ch.name in keep_text:
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
        print(f"archived: #{ch.name} -> #{new_name}")

    for vc in list(guild.voice_channels):
        if vc.name in keep_voice:
            continue
        await vc.delete(reason="Pre-launch sweep: voice has no history")
        print(f"deleted voice: {vc.name}")

    for cat in list(guild.categories):
        if cat.name not in keep_cats and not cat.channels:
            await cat.delete(reason="Pre-launch sweep: emptied category")
            print(f"deleted category: {cat.name}")


async def build(guild, citizen, owner):
    """Create or adopt everything in server_config.yaml, in order."""
    position = 0
    for cat_spec in CONFIG["categories"]:
        category = None
        cat_vis = cat_spec.get("visibility", "citizens")
        if cat_spec["name"]:
            cat_ow = overwrites_for(guild, citizen, owner, {"visibility": cat_vis})
            category = discord.utils.get(guild.categories, name=cat_spec["name"])
            if category is None:
                category = await guild.create_category(cat_spec["name"], overwrites=cat_ow)
                print(f"created category: {cat_spec['name']}")
            else:
                await category.edit(overwrites=cat_ow)
            await category.edit(position=position)
            position += 1

        for spec in cat_spec.get("channels") or []:
            is_voice = spec.get("type") == "voice"
            overwrites = overwrites_for(guild, citizen, owner, spec, default_vis=cat_vis)

            if is_voice:
                existing = discord.utils.get(guild.voice_channels, name=spec["name"])
                if existing is None:
                    await guild.create_voice_channel(
                        spec["name"], category=category,
                        user_limit=spec.get("user_limit", 0), overwrites=overwrites,
                    )
                    print(f"created voice: {spec['name']}")
                else:
                    await existing.edit(
                        category=category,
                        user_limit=spec.get("user_limit", 0),
                        overwrites=overwrites,
                    )
                    print(f"adopted voice: {spec['name']}")
            else:
                existing = discord.utils.get(guild.text_channels, name=spec["name"])
                if existing is None:
                    existing = next(
                        (
                            c
                            for c in guild.text_channels
                            if not c.name.startswith("archived_")
                            and base_name(c.name) == base_name(spec["name"])
                        ),
                        None,
                    )
                if existing is None:
                    await guild.create_text_channel(
                        spec["name"], category=category,
                        topic=spec.get("topic"), overwrites=overwrites,
                    )
                    print(f"created text: #{spec['name']}")
                else:
                    old_name = existing.name
                    await existing.edit(
                        name=spec["name"], category=category,
                        topic=spec.get("topic"), overwrites=overwrites,
                    )
                    print(
                        f"renamed text: #{old_name} -> #{spec['name']}"
                        if old_name != spec["name"]
                        else f"adopted text: #{spec['name']}"
                    )


def charter_markdown():
    """Charter text with italic lines as Discord subtext, dividers dropped."""
    out = []
    for line in CHARTER_TEXT.splitlines():
        s = line.strip()
        if s == "---":
            continue
        if s.startswith("*") and s.endswith("*"):
            out.append("-# " + s.strip("*"))
        else:
            out.append(line)
    return "\n".join(out).strip()


class CharterCard(discord.ui.LayoutView):
    """The charter as a single container: one accent stripe, sections
    divided by hairline separators. The chosen presentation (variant B)."""

    def __init__(self):
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_colour=ACCENT)
        for i, part in enumerate(re.split(r"\n(?=## )", charter_markdown())):
            if i:
                container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(part.strip()))
        self.add_item(container)


async def post_charter(guild):
    channel = next(
        (c for c in guild.text_channels if "charter" in c.name and c.category is None), None
    )
    if channel is None:
        print("WARNING: no top-level charter channel found, skipping charter post")
        return
    async for _ in channel.history(limit=1):
        print("charter already posted, skipping")
        return
    message = await channel.send(view=CharterCard())
    await message.pin(reason="The Founding Charter")
    print(f"charter posted and pinned in #{channel.name}")


async def enforce_order(guild):
    """Make sidebar order match config order exactly, per category and
    per bucket (Discord lists text channels first, then voice)."""
    for cat_spec in CONFIG["categories"]:
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


async def cleanup_stray_categories(guild):
    keep = {c["name"] for c in CONFIG["categories"] if c["name"]}
    for cat in list(guild.categories):
        if cat.name not in keep and not cat.channels:
            await cat.delete(reason="Emptied by rebuild")
            print(f"deleted category: {cat.name}")


@client.event
async def on_ready():
    exit_code = 0
    try:
        guild = client.get_guild(GUILD_ID)
        print(f"Clerk on duty in: {guild.name}")
        citizen = await ensure_citizen(guild)
        owner = guild.owner or await guild.fetch_member(guild.owner_id)
        print("-- sweep --")
        await sweep(guild, citizen, owner)
        print("-- build --")
        await build(guild, citizen, owner)
        await enforce_order(guild)
        await cleanup_stray_categories(guild)
        print("-- charter --")
        await post_charter(guild)
        print("-- done --")
    except Exception as e:
        print(f"FAILED: {e!r}")
        exit_code = 1
    finally:
        await client.close()
    if exit_code:
        raise SystemExit(exit_code)


client.run(TOKEN)
