"""Clarence the Clerk: the executive daemon of The Hangout.

The door:   sign the charter, receive the Key, the server opens.
The floor:  submit a bill (title/what/why), Clarence publishes it,
            opens a debate chamber (text + voice), runs an anonymous
            ballot, and closes the floor after voting.floor_hours.
At close:   tally (yes > no passes, ties fail), result posted, passed
            bills become numbered Acts in the gazette, final notes are
            preserved in a record thread, the chamber text channel is
            locked and archived, the voice channel and category removed.

Anonymity is toward members, not the machine: the clerk keeps records
to count and to permit edits, and never shows them to anyone.

Usage: .venv/bin/python clerk.py
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
import yaml
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

CONFIG = yaml.safe_load((HERE / "server_config.yaml").read_text())
FLOOR_HOURS = float(CONFIG.get("voting", {}).get("floor_hours", 48))

# State lives next to the code by default; on a host with a persistent
# disk (e.g. Render's /data), point CLERK_DATA_DIR there.
DATA = Path(os.environ.get("CLERK_DATA_DIR", HERE))
DATA.mkdir(parents=True, exist_ok=True)

SIGNATURES = DATA / "signatures.json"
STATE = DATA / "clerk_state.json"
BILLS = DATA / "bills.json"
ACTS = DATA / "acts.json"
ROLES = DATA / "roles.json"  # custom role registry: {role_id: {creator_id}}

ROLE_CREATE_MAX = 5  # roles one person may create
ROLE_WEAR_MAX = 5    # custom roles one person may wear

CITIZEN = "Key"  # the signing role is an object you receive, not a title
ACCENT = discord.Colour(0xE0A458)

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- storage ----------

def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2))


def now_utc():
    return datetime.now(timezone.utc)


# ---------- lookups ----------

def find_channel(guild, needle):
    return next((c for c in guild.text_channels if needle in c.name), None)


def charter_channel(guild):
    return next(
        (c for c in guild.text_channels if "charter" in c.name and c.category is None), None
    )


def reception_channel(guild):
    return next(
        (c for c in guild.text_channels if "reception" in c.name and c.category is None), None
    )


def key_role(guild):
    return discord.utils.get(guild.roles, name=CITIZEN)


def archive_category(guild):
    return next((c for c in guild.categories if "archive" in c.name.lower()), None)


def governance_category(guild):
    return next((c for c in guild.categories if "governance" in c.name.lower()), None)


def has_key(member):
    role = key_role(member.guild)
    return role is not None and role in member.roles


def bill_by(field, value):
    for bill in load_json(BILLS, []):
        if bill.get(field) == value:
            return bill
    return None


def update_bill(bill):
    bills = load_json(BILLS, [])
    for i, b in enumerate(bills):
        if b["no"] == bill["no"]:
            bills[i] = bill
            break
    save_json(BILLS, bills)


# ---------- rendering ----------

class Card(discord.ui.LayoutView):
    """One gold-striped container holding a list of text segments."""

    def __init__(self, segments):
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_colour=ACCENT)
        for i, segment in enumerate(segments):
            if i:
                container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(segment))
        self.add_item(container)


def chunk_text(text, limit=1900):
    """Split long text at paragraph/word boundaries, each piece <= limit."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    parts.append(text)
    return parts


# ---------- the door ----------

class SignView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Sign the charter",
        emoji="🖋️",
        style=discord.ButtonStyle.success,
        custom_id="clerk:sign",
    )
    async def sign(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user
        role = key_role(guild)
        if role is None:
            return await interaction.response.send_message(
                "The Key role is missing. Run build_server.py first.", ephemeral=True
            )
        if role in member.roles:
            return await interaction.response.send_message(
                "You have already signed. The record remembers.", ephemeral=True
            )
        await member.add_roles(role, reason="Signed the charter")
        signatures = load_json(SIGNATURES, [])
        signatures.append(
            {
                "id": member.id,
                "name": member.name,
                "display": member.display_name,
                "signed_at": now_utc().isoformat(),
            }
        )
        save_json(SIGNATURES, signatures)
        print(f"signature: {member.display_name} ({member.id})")
        await interaction.response.send_message(
            "Signed. Here is your key; the Hangout is open to you.", ephemeral=True
        )


# ---------- the chamber ----------

def chamber_overwrites(guild):
    return {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        key_role(guild): discord.PermissionOverwrite(view_channel=True),
    }


async def hidden_overwrites(guild):
    owner = guild.owner or await guild.fetch_member(guild.owner_id)
    return {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        key_role(guild): discord.PermissionOverwrite(view_channel=False),
        owner: discord.PermissionOverwrite(view_channel=True, send_messages=False),
    }


def bill_name(number, title, prefix="", limit=100):
    """Bill-<number>-<as much of the title as fits>, with optional emoji
    prefix, within Discord's 100-char channel name limit."""
    stem = f"Bill-{number}"
    slug = re.sub(r"-{2,}", "-", re.sub(r"\s+", "-", title.strip()))
    room = limit - len(prefix) - len(stem) - 1
    name = f"{stem}-{slug[:room]}".rstrip("-") if room > 0 and slug else stem
    return prefix + name


async def open_chamber(guild, number, title):
    ow = chamber_overwrites(guild)
    position = None
    gov = governance_category(guild)
    if gov:
        position = gov.position
    category = await guild.create_category(
        bill_name(number, title, "🗳️ "), overwrites=ow, position=position
    )
    text = await guild.create_text_channel(
        bill_name(number, title, "💬・"),
        category=category,
        topic=f"Debate chamber for Bill No. {number}: {title}",
        overwrites=ow,
    )
    voice = await guild.create_voice_channel(
        bill_name(number, title, "🔊 "), category=category, overwrites=ow
    )
    return category, text, voice


# ---------- notes ----------

NOTES_PROMPT = (
    "-# Have a case to make? File your position below: named or "
    "anonymous, one slot of each, editable until the floor closes."
)


def render_note(kind, display, text):
    who = "Anonymous" if kind == "anon" else display
    return f"📝 **{who}**\n{text}"


class NoteModal(discord.ui.Modal):
    def __init__(self, bill, kind, existing):
        label = "Anonymous note" if kind == "anon" else "Note under your name"
        super().__init__(title=f"{label}: Bill {bill['no']}"[:45])
        self.bill_no = bill["no"]
        self.kind = kind
        self.note = discord.ui.TextInput(
            label="Your note",
            style=discord.TextStyle.paragraph,
            max_length=1800,
            default=existing,
            placeholder="Say the thing you want on the record.",
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        bill = bill_by("no", self.bill_no)
        if bill is None or bill["status"] != "on_floor":
            return await interaction.followup.send("This floor has closed.", ephemeral=True)
        uid = str(interaction.user.id)
        notes = bill.setdefault("notes", {})
        slots = notes.setdefault(uid, {})
        text = str(self.note)
        channel = interaction.guild.get_thread(bill["notes_thread_id"])
        if channel is None:
            channel = await interaction.guild.fetch_channel(bill["notes_thread_id"])
        existing = slots.get(self.kind)
        if existing:
            try:
                message = await channel.fetch_message(existing["message_id"])
                await message.edit(
                    content=render_note(self.kind, existing.get("display", ""), text)
                )
            except discord.NotFound:
                pass
            existing["text"] = text
            existing["edited_at"] = now_utc().isoformat()
        else:
            display = interaction.user.display_name
            message = await channel.send(render_note(self.kind, display, text))
            slots[self.kind] = {
                "text": text,
                "display": display,
                "message_id": message.id,
                "first_at": now_utc().isoformat(),
            }
        update_bill(bill)
        await interaction.followup.send(
            "Noted. You can edit it until the floor closes.", ephemeral=True
        )


class NotesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _open(self, interaction, kind):
        if not has_key(interaction.user):
            return await interaction.response.send_message(
                "A key is required. Sign the charter at the door first.", ephemeral=True
            )
        bill = bill_by("notes_message_id", interaction.message.id)
        if bill is None or bill["status"] != "on_floor":
            return await interaction.response.send_message(
                "This floor has closed.", ephemeral=True
            )
        slot = bill.get("notes", {}).get(str(interaction.user.id), {}).get(kind)
        await interaction.response.send_modal(
            NoteModal(bill, kind, slot["text"] if slot else None)
        )

    @discord.ui.button(
        label="Note (named)", emoji="📝",
        style=discord.ButtonStyle.primary, custom_id="clerk:note_named",
    )
    async def named(self, interaction, button):
        await self._open(interaction, "named")

    @discord.ui.button(
        label="Note (anonymous)", emoji="🕶️",
        style=discord.ButtonStyle.secondary, custom_id="clerk:note_anon",
    )
    async def anon(self, interaction, button):
        await self._open(interaction, "anon")


# ---------- ballots ----------

class BallotView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _vote(self, interaction, choice):
        if not has_key(interaction.user):
            return await interaction.response.send_message(
                "A key is required to vote. Sign the charter at the door first.",
                ephemeral=True,
            )
        bill = bill_by("ballot_message_id", interaction.message.id)
        if bill is None or bill["status"] != "on_floor":
            return await interaction.response.send_message(
                "This floor has closed.", ephemeral=True
            )
        ballots = bill.setdefault("ballots", {})
        uid = str(interaction.user.id)
        if choice is None:
            if uid in ballots:
                del ballots[uid]
                update_bill(bill)
                return await interaction.response.send_message(
                    "Ballot retracted.", ephemeral=True
                )
            return await interaction.response.send_message(
                "You have no ballot to retract.", ephemeral=True
            )
        ballots[uid] = choice
        update_bill(bill)
        await interaction.response.send_message(
            f"Your ballot: **{choice}**. You can change it until the floor closes. "
            f"Nobody, including the author, will see how you voted.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Yes", emoji="✅",
        style=discord.ButtonStyle.success, custom_id="clerk:vote_yes",
    )
    async def yes(self, interaction, button):
        await self._vote(interaction, "yes")

    @discord.ui.button(
        label="No", emoji="❌",
        style=discord.ButtonStyle.danger, custom_id="clerk:vote_no",
    )
    async def no(self, interaction, button):
        await self._vote(interaction, "no")

    @discord.ui.button(
        label="Retract", style=discord.ButtonStyle.secondary, custom_id="clerk:vote_retract",
    )
    async def retract(self, interaction, button):
        await self._vote(interaction, None)


# ---------- submitting bills ----------

class BillModal(discord.ui.Modal, title="Submit a bill"):
    bill_title = discord.ui.TextInput(
        label="Title",
        style=discord.TextStyle.short,
        placeholder="A short name for the law.",
        max_length=100,
    )
    what = discord.ui.TextInput(
        label="What",
        style=discord.TextStyle.paragraph,
        placeholder="The text of the bill. What should become law?",
        max_length=4000,
    )
    why = discord.ui.TextInput(
        label="Why",
        style=discord.TextStyle.paragraph,
        placeholder="Your reasons. A bill without reasons is not a bill.",
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        floor = find_channel(guild, "the-floor")
        if floor is None:
            return await interaction.followup.send(
                "The floor is missing. Run build_server.py first.", ephemeral=True
            )
        bills = load_json(BILLS, [])
        number = len(bills) + 1
        title = str(self.bill_title)
        what, why = str(self.what), str(self.why)
        ends_at = now_utc() + timedelta(hours=FLOOR_HOURS)

        category, text, voice = await open_chamber(guild, number, title)

        stamp = await floor.send(
            view=Card([f"## Bill No. {number}: {title}\nSubmitted by {interaction.user.mention}"])
        )
        notes_thread = await stamp.create_thread(
            name=(bill_name(number, title) + ": notes")[:100]
        )
        notes_msg = await notes_thread.send(NOTES_PROMPT, view=NotesView())
        for label, body in (("What", what), ("Why", why)):
            for i, piece in enumerate(chunk_text(body)):
                prefix = f"### {label}\n" if i == 0 else ""
                await floor.send(prefix + piece)
        ballot = await floor.send(
            f"**Ballot** for Bill No. {number}. Cast, change, or retract until the floor "
            f"closes <t:{int(ends_at.timestamp())}:R>. Debate in {text.mention}. "
            f"Results appear at close; individual votes never do.",
            view=BallotView(),
        )
        bills.append(
            {
                "no": number,
                "title": title,
                "author_id": interaction.user.id,
                "author": interaction.user.display_name,
                "what": what,
                "why": why,
                "message_id": stamp.id,
                "ballot_message_id": ballot.id,
                "notes_message_id": notes_msg.id,
                "notes_thread_id": notes_thread.id,
                "chamber_category_id": category.id,
                "chamber_text_id": text.id,
                "chamber_voice_id": voice.id,
                "submitted_at": now_utc().isoformat(),
                "ends_at": ends_at.isoformat(),
                "status": "on_floor",
                "ballots": {},
                "notes": {},
            }
        )
        save_json(BILLS, bills)
        print(f"bill no. {number} ({title}) by {interaction.user.display_name}")
        await interaction.followup.send(
            f"Filed. Bill No. {number} is on the floor: {floor.mention}, "
            f"debate in {text.mention}.",
            ephemeral=True,
        )


class SubmitBillView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Submit a bill",
        emoji="🖋️",
        style=discord.ButtonStyle.primary,
        custom_id="clerk:bill",
    )
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_key(interaction.user):
            return await interaction.response.send_message(
                "A key is required to file bills. Sign the charter at the door first.",
                ephemeral=True,
            )
        await interaction.response.send_modal(BillModal())


# ---------- closing the floor ----------

async def close_bill(guild, bill):
    ballots = bill.get("ballots", {})
    yes = sum(1 for v in ballots.values() if v == "yes")
    no = sum(1 for v in ballots.values() if v == "no")
    passed = yes > no
    verdict = "Passed" if passed else "Failed"
    bill["status"] = "passed" if passed else "failed"
    bill["tally"] = {"yes": yes, "no": no}
    bill["closed_at"] = now_utc().isoformat()

    floor = find_channel(guild, "the-floor")
    gazette = find_channel(guild, "gazette")

    act_line = ""
    if passed and gazette:
        acts = load_json(ACTS, [])
        act_no = len(acts) + 1
        acts.append(
            {
                "act": act_no,
                "bill": bill["no"],
                "title": bill["title"],
                "author": bill["author"],
                "what": bill["what"],
                "tally": bill["tally"],
                "passed_at": bill["closed_at"],
            }
        )
        save_json(ACTS, acts)
        bill["act"] = act_no
        await gazette.send(view=Card([f"## Act {act_no}: {bill['title']}"]))
        for piece in chunk_text(bill["what"]):
            await gazette.send(piece)
        await gazette.send(
            view=Card([
                f"-# From Bill No. {bill['no']} by {bill['author']}. "
                f"Passed ✅ {yes} / ❌ {no}."
            ])
        )
        act_line = f"\n-# Recorded as Act {act_no} in the gazette."

    if floor:
        await floor.send(
            view=Card([
                f"## Bill No. {bill['no']}: {bill['title']}\n"
                f"**{verdict}**  ✅ {yes} / ❌ {no}{act_line}"
            ])
        )
        # seal the notes thread: it already is the record
        thread_id = bill.get("notes_thread_id")
        if thread_id:
            try:
                thread = guild.get_thread(thread_id) or await guild.fetch_channel(thread_id)
                count = sum(len(s) for s in bill.get("notes", {}).values())
                await thread.send(
                    f"*Floor closed. {count} note(s) on record.*"
                    if count
                    else "*Floor closed. No notes were filed.*"
                )
                await thread.edit(archived=True, locked=True)
            except discord.HTTPException as e:
                print(f"sealing notes thread failed for bill {bill['no']}: {e!r}")
        # close the ballot message
        try:
            ballot_msg = await floor.fetch_message(bill["ballot_message_id"])
            await ballot_msg.edit(
                content=f"**Ballot closed** for Bill No. {bill['no']}. "
                f"{verdict}: ✅ {yes} / ❌ {no}.",
                view=None,
            )
        except discord.HTTPException:
            pass

    # archive the chamber: text is kept (owner-visible), voice evaporates
    archive = archive_category(guild)
    hidden = await hidden_overwrites(guild)
    text = guild.get_channel(bill["chamber_text_id"])
    if text and archive:
        await text.edit(
            name=bill_name(bill["no"], bill["title"], "archived_"),
            category=archive,
            sync_permissions=False,
            overwrites=hidden,
        )
    voice = guild.get_channel(bill["chamber_voice_id"])
    if voice:
        await voice.delete(reason="Floor closed; voice is never recorded")
    category = guild.get_channel(bill["chamber_category_id"])
    if category and not category.channels:
        await category.delete(reason="Floor closed")

    update_bill(bill)
    print(f"bill no. {bill['no']} closed: {verdict.lower()} ({yes}/{no})")


@tasks.loop(seconds=60)
async def check_floor():
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    for bill in load_json(BILLS, []):
        if bill.get("status") != "on_floor" or "ends_at" not in bill:
            continue
        if datetime.fromisoformat(bill["ends_at"]) <= now_utc():
            try:
                await close_bill(guild, bill)
            except Exception as e:
                print(f"failed to close bill {bill['no']}: {e!r}")


# ---------- custom roles (purely aesthetic) ----------

def role_registry():
    return load_json(ROLES, {})


def created_by(user_id):
    return [rid for rid, meta in role_registry().items() if meta["creator_id"] == user_id]


def worn_custom(member):
    registry = role_registry()
    return [r for r in member.roles if str(r.id) in registry]


def custom_stack(guild):
    """All custom roles, highest position first (the color stack)."""
    registry = role_registry()
    return sorted(
        (r for r in guild.roles if str(r.id) in registry),
        key=lambda r: r.position,
        reverse=True,
    )


def parse_colour(value):
    value = value.strip().lstrip("#")
    return discord.Colour.from_str(f"#{value}")


def roles_channel(guild):
    return next((c for c in guild.text_channels if "roles" in c.name), None)


async def update_wardrobe(guild):
    channel = roles_channel(guild)
    if channel is None:
        return
    state = load_json(STATE, {})
    stack = custom_stack(guild)
    registry = role_registry()
    if stack:
        lines = ["## The wardrobe", "-# Highest wins the name color when worn together."]
        for r in stack:
            creator = guild.get_member(registry[str(r.id)]["creator_id"])
            who = creator.display_name if creator else "someone departed"
            wearing = len([m for m in r.members if not m.bot])
            lines.append(f"{r.mention}: {wearing} wearing, made by {who}")
        content = "\n".join(lines)[:1990]
    else:
        content = "## The wardrobe\n-# Empty. Someone should make the first color."
    msg_id = state.get("wardrobe_message_id")
    if msg_id:
        try:
            message = await channel.fetch_message(msg_id)
            return await message.edit(content=content)
        except discord.NotFound:
            pass
    message = await channel.send(content)
    state["wardrobe_message_id"] = message.id
    save_json(STATE, state)


class RoleCreateModal(discord.ui.Modal, title="Create a role"):
    name = discord.ui.TextInput(label="Name", max_length=100, placeholder="What it says")
    color = discord.ui.TextInput(
        label="Color (hex)", max_length=7, placeholder="#ff9d2e", min_length=4
    )

    async def on_submit(self, interaction: discord.Interaction):
        name = str(self.name).strip()[:100]
        if not name or name.lower() in {CITIZEN.lower(), "clerk", "@everyone", "@here"}:
            return await interaction.response.send_message("Pick another name.", ephemeral=True)
        try:
            colour = parse_colour(str(self.color))
        except (ValueError, IndexError):
            return await interaction.response.send_message(
                "That is not a hex color. Try something like #ff9d2e.", ephemeral=True
            )
        if len(created_by(interaction.user.id)) >= ROLE_CREATE_MAX:
            return await interaction.response.send_message(
                f"You already created {ROLE_CREATE_MAX} roles. Delete one to make room.",
                ephemeral=True,
            )
        role = await interaction.guild.create_role(
            name=name, colour=colour, permissions=discord.Permissions.none(),
            mentionable=False, hoist=False,
            reason=f"Custom role by {interaction.user.display_name}",
        )
        registry = role_registry()
        registry[str(role.id)] = {"creator_id": interaction.user.id}
        save_json(ROLES, registry)
        wearing = ""
        if len(worn_custom(interaction.user)) < ROLE_WEAR_MAX:
            await interaction.user.add_roles(role, reason="Creator wears their creation")
            wearing = " You are wearing it."
        await interaction.response.send_message(f"Created {role.mention}.{wearing}", ephemeral=True)
        await update_wardrobe(interaction.guild)


class RoleEditModal(discord.ui.Modal, title="Edit role"):
    def __init__(self, role):
        super().__init__()
        self.role_id = role.id
        self.name = discord.ui.TextInput(label="Name", max_length=100, default=role.name)
        self.color = discord.ui.TextInput(
            label="Color (hex)", max_length=7, min_length=4, default=f"#{role.colour.value:06x}"
        )
        self.add_item(self.name)
        self.add_item(self.color)

    async def on_submit(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if role is None:
            return await interaction.response.send_message("That role is gone.", ephemeral=True)
        try:
            colour = parse_colour(str(self.color))
        except (ValueError, IndexError):
            return await interaction.response.send_message(
                "That is not a hex color.", ephemeral=True
            )
        await role.edit(name=str(self.name).strip()[:100] or role.name, colour=colour)
        await interaction.response.send_message(f"Updated {role.mention}.", ephemeral=True)
        await update_wardrobe(interaction.guild)


def _role_options(roles, member=None):
    options = []
    for r in roles[:25]:
        marks = []
        if member is not None and r in member.roles:
            marks.append("wearing")
        options.append(
            discord.SelectOption(
                label=r.name[:100],
                value=str(r.id),
                description=(", ".join(marks) or None),
            )
        )
    return options


class WearView(discord.ui.View):
    """Ephemeral toggle picker: tap a role to wear it, tap again to shed."""

    def __init__(self, guild, member):
        super().__init__(timeout=300)
        stack = custom_stack(guild)
        self.select = discord.ui.Select(
            placeholder="Pick a role to wear or shed",
            options=_role_options(stack, member),
        )
        self.select.callback = self.toggle
        self.add_item(self.select)

    async def toggle(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(int(self.select.values[0]))
        if role is None or str(role.id) not in role_registry():
            return await interaction.response.send_message("That role is gone.", ephemeral=True)
        member = interaction.user
        if role in member.roles:
            await member.remove_roles(role, reason="Shed a custom role")
            verdict = f"Shed {role.mention}."
        elif len(worn_custom(member)) >= ROLE_WEAR_MAX:
            verdict = f"You are wearing {ROLE_WEAR_MAX} already. Shed one first."
        else:
            await member.add_roles(role, reason="Wearing a custom role")
            verdict = f"Wearing {role.mention}."
        await interaction.response.edit_message(
            content=verdict, view=WearView(interaction.guild, member)
        )
        await update_wardrobe(interaction.guild)


class ManageActionsView(discord.ui.View):
    def __init__(self, role):
        super().__init__(timeout=300)
        self.role_id = role.id

    def _role(self, interaction):
        role = interaction.guild.get_role(self.role_id)
        if role is None or str(role.id) not in role_registry():
            return None
        return role

    @discord.ui.button(label="Rename / recolor", style=discord.ButtonStyle.primary)
    async def edit(self, interaction, button):
        role = self._role(interaction)
        if role is None:
            return await interaction.response.send_message("That role is gone.", ephemeral=True)
        await interaction.response.send_modal(RoleEditModal(role))

    @discord.ui.button(label="Raise", style=discord.ButtonStyle.secondary)
    async def up(self, interaction, button):
        await self._shift(interaction, +1)

    @discord.ui.button(label="Lower", style=discord.ButtonStyle.secondary)
    async def down(self, interaction, button):
        await self._shift(interaction, -1)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete(self, interaction, button):
        role = self._role(interaction)
        if role is None:
            return await interaction.response.send_message("That role is gone.", ephemeral=True)
        registry = role_registry()
        del registry[str(role.id)]
        save_json(ROLES, registry)
        await role.delete(reason=f"Deleted by creator {interaction.user.display_name}")
        await interaction.response.edit_message(content="Deleted.", view=None)
        await update_wardrobe(interaction.guild)

    async def _shift(self, interaction, direction):
        role = self._role(interaction)
        if role is None:
            return await interaction.response.send_message("That role is gone.", ephemeral=True)
        stack = sorted(custom_stack(interaction.guild), key=lambda r: r.position)
        i = stack.index(role)
        j = i + direction
        if j < 0 or j >= len(stack):
            return await interaction.response.send_message(
                "It is already at that end of the stack.", ephemeral=True
            )
        other = stack[j]
        await interaction.guild.edit_role_positions(
            {role: other.position, other: role.position},
            reason=f"Reordered by {interaction.user.display_name}",
        )
        await interaction.response.send_message(
            f"{role.mention} moved {'up' if direction > 0 else 'down'}. "
            f"The higher role's color wins when worn together.",
            ephemeral=True,
        )
        await update_wardrobe(interaction.guild)


class ManageView(discord.ui.View):
    def __init__(self, guild, member):
        super().__init__(timeout=300)
        mine = [r for r in custom_stack(guild) if role_registry()[str(r.id)]["creator_id"] == member.id]
        self.select = discord.ui.Select(
            placeholder="Pick one of your roles", options=_role_options(mine)
        )
        self.select.callback = self.pick
        self.add_item(self.select)

    async def pick(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(int(self.select.values[0]))
        if role is None:
            return await interaction.response.send_message("That role is gone.", ephemeral=True)
        await interaction.response.edit_message(
            content=f"Managing {role.mention}.", view=ManageActionsView(role)
        )


class RolesHomeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Create a role", emoji="🎨",
        style=discord.ButtonStyle.primary, custom_id="clerk:role_create",
    )
    async def create(self, interaction, button):
        if not has_key(interaction.user):
            return await interaction.response.send_message(
                "A key is required. Sign the charter first.", ephemeral=True
            )
        if len(created_by(interaction.user.id)) >= ROLE_CREATE_MAX:
            return await interaction.response.send_message(
                f"You already created {ROLE_CREATE_MAX} roles. Delete one to make room.",
                ephemeral=True,
            )
        await interaction.response.send_modal(RoleCreateModal())

    @discord.ui.button(
        label="Wear / shed", emoji="🧥",
        style=discord.ButtonStyle.secondary, custom_id="clerk:role_wear",
    )
    async def wear(self, interaction, button):
        if not has_key(interaction.user):
            return await interaction.response.send_message(
                "A key is required. Sign the charter first.", ephemeral=True
            )
        if not custom_stack(interaction.guild):
            return await interaction.response.send_message(
                "The wardrobe is empty. Create the first role.", ephemeral=True
            )
        await interaction.response.send_message(
            "Tap to wear, tap again to shed.",
            view=WearView(interaction.guild, interaction.user),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Manage mine", emoji="🧰",
        style=discord.ButtonStyle.secondary, custom_id="clerk:role_manage",
    )
    async def manage(self, interaction, button):
        if not created_by(interaction.user.id):
            return await interaction.response.send_message(
                "You have not created any roles yet.", ephemeral=True
            )
        await interaction.response.send_message(
            "Your creations:",
            view=ManageView(interaction.guild, interaction.user),
            ephemeral=True,
        )


# ---------- pinned buttons ----------

async def ensure_button_message(channel, state_key, content, view):
    if channel is None:
        print(f"WARNING: channel for {state_key} missing; button not posted")
        return
    state = load_json(STATE, {})
    msg_id = state.get(state_key)
    if msg_id:
        try:
            await channel.fetch_message(msg_id)
            return
        except discord.NotFound:
            pass
    message = await channel.send(content, view=view)
    state[state_key] = message.id
    save_json(STATE, state)
    print(f"button posted in #{channel.name}")


# ---------- health endpoint ----------
# Binds only when PORT is set (Render provides it). Serves /healthz with
# read-only vitals; the future dashboard grows from here.

async def start_web():
    port = int(os.environ.get("PORT", 0))
    if not port:
        return

    async def healthz(request):
        bills = load_json(BILLS, [])
        ready = bot.is_ready()
        guild = bot.get_guild(GUILD_ID) if ready else None
        return web.json_response(
            {
                "status": "ok",
                "clerk": str(bot.user) if ready else None,
                "guild": guild.name if guild else None,
                "ready": ready,
                "latency_ms": round(bot.latency * 1000) if ready else None,
                "open_bills": sum(1 for b in bills if b.get("status") == "on_floor"),
                "acts": len(load_json(ACTS, [])),
                "signatures": len(load_json(SIGNATURES, [])),
                "custom_roles": len(load_json(ROLES, {})),
            }
        )

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"health endpoint listening on :{port}/healthz")


# ---------- lifecycle ----------

@bot.event
async def setup_hook():
    await start_web()
    bot.add_view(SignView())
    bot.add_view(SubmitBillView())
    bot.add_view(BallotView())
    bot.add_view(NotesView())
    bot.add_view(RolesHomeView())
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)


async def ensure_furniture(guild):
    """Post any missing clerk furniture. Runs at boot and periodically,
    so channels created after boot get furnished without a restart."""
    await ensure_button_message(
        charter_channel(guild),
        "sign_message_id",
        "*Signing accepts the seeded arrangements as a starting point, "
        "nothing more. Any of it can be overwritten by a later Act.*",
        SignView(),
    )
    await ensure_button_message(
        find_channel(guild, "submit-a-bill"),
        "bill_message_id",
        "*State what should become law and why. The clerk files it, "
        "the floor decides. Authorship is public; laws do not have "
        "anonymous authors.*",
        SubmitBillView(),
    )
    await ensure_button_message(
        roles_channel(guild),
        "roles_home_id",
        "*Self-service: a role is a name and a color, nothing more. "
        "Create up to five, wear up to five, yours or anyone's.*",
        RolesHomeView(),
    )
    await update_wardrobe(guild)


@tasks.loop(seconds=300)
async def furniture_loop():
    guild = bot.get_guild(GUILD_ID)
    if guild:
        try:
            await ensure_furniture(guild)
        except Exception as e:
            print(f"furniture check failed: {e!r}")


@bot.event
async def on_ready():
    print(f"On duty as {bot.user}")
    guild = bot.get_guild(GUILD_ID)
    if guild:
        await ensure_furniture(guild)
    if not check_floor.is_running():
        check_floor.start()
    if not furniture_loop.is_running():
        furniture_loop.start()


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot or member.guild.id != GUILD_ID:
        return
    reception = reception_channel(member.guild)
    charter = charter_channel(member.guild)
    if reception:
        charter_ref = charter.mention if charter else "the charter"
        await reception.send(
            f"Welcome, {member.mention}. The charter is pinned at the door, "
            f"in {charter_ref}; sign it and you get your key to the Hangout. "
            f"Until then, this desk can hear you."
        )


bot.run(TOKEN)
