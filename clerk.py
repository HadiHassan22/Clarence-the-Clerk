"""Eugene: the engine of whichever server he is in.

Nothing here names a house. The server's own name comes from `guild.name`
wherever one is needed -- proposal text, the character prompt, the health
card -- and its rooms and roles are bound by id through `/setup`. He was
written for one server and it is no longer that server; anything that
hardcodes a name is a bug, not a default.

Roles:      given by people, not earned at a button. Eugene reads them.
Proposing: say what should change (title/what/why), Eugene publishes it,
            opens a debate chamber (text + voice), and runs an anonymous
            ballot.
Voting:     thresholds count against the roster, not against turnout, so a
            vote ends the moment its result is settled rather than when the
            clock runs out. A majority carries most things; a removal wants
            three quarters. voting.floor_hours is only the backstop for a
            vote nobody finishes.
At close:   result posted, passed proposals become numbered decisions in
            the record, final notes are preserved in a thread, the chamber
            text channel is locked and archived, the voice channel and
            category removed. Choice ballots (author supplies 2-10 options)
            need a strict majority of votes cast; otherwise a runoff opens
            with the leading options, decided by plurality. Authors cannot
            file notes on their own proposals.

Anonymity is toward people, not the machine: Eugene keeps records to count
and to permit edits, and never shows them to anyone.

Usage: .venv/bin/python clerk.py
"""

import asyncio
import json
import logging
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

import bindings
import brain
import builder
import duties
import providers
import roster
import settings
import toolbox

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

STATE = DATA / "clerk_state.json"
BILLS = DATA / "bills.json"
ACTS = DATA / "acts.json"
ROLES = DATA / "roles.json"  # custom role registry: {role_id: {creator_id}}

KICK_MIN_YES = 3     # removal: never fewer, however small the roster
ROLE_CREATE_MAX = 1  # roles one person may create
ROLE_WEAR_MAX = 5    # custom roles one person may wear

log = logging.getLogger("clerk")

COOPERATIVE = "Cooperative"  # holds a vote: whoever picked up a chore
MEMBER = "Member"            # in the room, no vote; unused while all of
                             # us are in the cooperative
NERD = "nerd"    # opt-in subscription to the bot-health channel, not a rank

# Said to somebody who is not in the cooperative. It names the way in on
# purpose: a refusal that only says no leaves a new arrival stuck, which is
# exactly how the first install went -- the person who installed Eugene was
# outside, and nothing anywhere told them how to get inside.
NOT_INSIDE = (
    "Only the cooperative files proposals here. Anyone already inside can "
    "put you up with `/invite`; whoever runs the place can hand it over "
    "with `/setup grant`."
)
ACCENT = discord.Colour(0xE0A458)
BOOT_AT = datetime.now(timezone.utc)


def running_commit():
    """Whichever host we are on, name the commit. The deploy announcement
    in bot-health fires on this changing, so a host whose variable we do
    not know would quietly never announce anything."""
    for variable in ("CLERK_COMMIT", "RAILWAY_GIT_COMMIT_SHA", "RENDER_GIT_COMMIT",
                     "SOURCE_VERSION", "HEROKU_SLUG_COMMIT"):
        value = os.environ.get(variable)
        if value:
            return value[:7]
    return "local"


COMMIT = running_commit()

intents = discord.Intents.default()
intents.members = True
# The brain needs message text, and a server can now wake its brain at any
# moment with /setup brain, so the intent is asked for up front rather
# than decided from the environment at boot. It is privileged: turn it on
# in the Developer Portal under Bot -> Privileged Gateway Intents. A host
# that cannot, or that wants a clerk with no brain at all, sets
# CLERK_MESSAGE_CONTENT=0 and gets the door and the floor and no talking.
intents.message_content = os.environ.get("CLERK_MESSAGE_CONTENT", "1") != "0"
bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- storage ----------

def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path, data):
    """Atomic: a crash or redeploy mid-write must never truncate state."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def now_utc():
    return datetime.now(timezone.utc)


# ---------- lookups ----------

def base_name(name):
    """Channel identity ignoring the emoji prefix: '🥗・food' -> 'food'."""
    return name.split("・", 1)[-1].strip().lower()


# What each room used to be called. Lookups try the current name first
# and fall back, so a deploy works either side of the server rebuild and
# keeps working if nobody ever gets round to it.
CHANNEL_ALIASES = {
    "proposals": ("propose", "submit-a-bill"),
    "health": ("bot-health",),
    "votes": ("votes", "the-floor"),
    "propose": ("propose", "submit-a-bill"),
    "decisions": ("decisions", "gazette"),
}


def find_channel(guild, needle):
    """Exact base-name match first so a channel named 'my-votes-notes'
    cannot shadow the votes room; substring only as a fallback."""
    names = CHANNEL_ALIASES.get(needle.lower(), (needle.lower(),))
    for name in names:
        exact = next(
            (c for c in guild.text_channels if base_name(c.name) == name), None
        )
        if exact:
            return exact
    for name in names:
        loose = next(
            (c for c in guild.text_channels if name in c.name.lower()), None
        )
        if loose:
            return loose
    return None


def invite_channel(guild):
    """Somewhere an invite link can point. The bound welcome room if there is
    one, else whatever Discord already treats as the front door, else the
    first channel Eugene is actually allowed to create an invite in."""
    picked = room(guild, "welcome") or guild.system_channel
    me = guild.me
    if picked is not None and picked.permissions_for(me).create_instant_invite:
        return picked
    return next(
        (c for c in guild.text_channels
         if c.permissions_for(me).create_instant_invite),
        None,
    )


def room(guild, key):
    """A channel by the job it does.

    The binding set through `/setup rooms` always wins. Falling back to a name
    match keeps a server that has not been pointed at anything yet working
    exactly as it did before, so binding is an upgrade rather than a cutover.
    """
    if guild is None:
        return None
    return bindings.channel(guild, key) or find_channel(guild, key)


def cooperative_role(guild):
    return (
        bindings.role(guild, "cooperative")
        or discord.utils.get(guild.roles, name=COOPERATIVE)
    )


def member_role(guild):
    return (
        bindings.role(guild, "member")
        or discord.utils.get(guild.roles, name=MEMBER)
    )


def archive_category(guild):
    return next((c for c in guild.categories if "archive" in c.name.lower()), None)


def governance_category(guild):
    return next((c for c in guild.categories if "governance" in c.name.lower()), None)


def health_channel(guild):
    return room(guild, "health")


async def health_log(guild, text):
    """Post an event line to bot-health; never let reporting break the op."""
    channel = health_channel(guild)
    if channel:
        try:
            await channel.send(text[:1900])
        except discord.HTTPException:
            pass


def health_content(guild):
    bills = load_json(BILLS, [])
    open_bills = [b for b in bills if b.get("status") == "on_floor"]
    next_close = min(
        (b["ends_at"] for b in open_bills if "ends_at" in b), default=None
    )
    lines = [
        "## Eugene's vitals",
        f"Commit: `{COMMIT}`",
        f"On duty since <t:{int(BOOT_AT.timestamp())}:R>",
        f"Gateway latency: {round(bot.latency * 1000)}ms" if bot.is_ready() else "Gateway: connecting",
        f"Open proposals: {len(open_bills)}"
        + (
            f" (next close <t:{int(datetime.fromisoformat(next_close).timestamp())}:R>)"
            if next_close
            else ""
        ),
        f"Decisions: {len(load_json(ACTS, []))} "
        f"| Custom roles: {len(load_json(ROLES, {}))}",
        f"Vote window: {FLOOR_HOURS:g}h",
        brain.spend_line(guild.id),
        f"-# Updated <t:{int(now_utc().timestamp())}:R>. "
        f"Opt out with Nerd mode in the roles channel.",
    ]
    return "\n".join(lines)


async def update_health(guild):
    channel = health_channel(guild)
    if channel is None:
        return
    state = load_json(STATE, {})
    content = health_content(guild)
    msg_id = state.get("health_message_id")
    if msg_id:
        try:
            message = await channel.fetch_message(msg_id)
            return await message.edit(content=content)
        except discord.NotFound:
            pass
    message = await channel.send(content)
    try:
        await message.pin(reason="Eugene's vitals")
    except discord.HTTPException:
        pass
    state["health_message_id"] = message.id
    save_json(STATE, state)


def in_cooperative(member):
    role = cooperative_role(member.guild)
    return role is not None and role in member.roles


def in_room(member):
    """Anyone who is in at all: the cooperative, or a member without a vote.
    The cooperative is a superset, so holding either is enough."""
    if in_cooperative(member):
        return True
    role = member_role(member.guild)
    return role is not None and role in member.roles


# Who a ballot is open to. Cooperative business is the default, because a
# thing that forgets to say what it is should be the closed kind, never the
# open one.
COOPERATIVE_ONLY = "cooperative"
EVERYONE = "everyone"


def audience_of(bill):
    return EVERYONE if bill.get("audience") == EVERYONE else COOPERATIVE_ONLY


def may_vote(bill, member):
    """The single gate for every ballot in the server.

    Deliberately keyed on the proposal, never on which channel the message
    happens to sit in. Channel permissions are the first line and they are
    not trusted as the only one: a message that gets moved, a permission
    that drifts during a rebuild, or a stale view surviving a restart must
    not become a way into a vote you are not part of.
    """
    if getattr(member, "bot", False):
        return False
    if audience_of(bill) == EVERYONE:
        return in_room(member)
    return in_cooperative(member)


def refusal_for(bill):
    return (
        "You need to be in the room to vote in this one."
        if audience_of(bill) == EVERYONE
        else "This one is the cooperative's to decide. Public polls are in "
        "#polls, and whatever gets decided lands in #decisions."
    )


# ---------- which house ----------
# The daemon keeps one house today, named by GUILD_ID. Everything that
# needs to know which one asks here rather than reading the environment,
# so the day it keeps several these three functions change and nothing
# else does.

def home_guild():
    return bot.get_guild(GUILD_ID)


def serves(guild):
    return guild is not None and guild.id == GUILD_ID


def resolve_guild(message):
    """The server a message is addressed to. In a channel it is plain; in
    a direct message there is no guild at all, so Eugene answers for
    the server he keeps, and only to someone who is in it."""
    if message.guild is not None:
        return message.guild if serves(message.guild) else None
    guild = home_guild()
    if guild is not None and guild.get_member(message.author.id) is not None:
        return guild
    return None


def bill_by(field, value):
    for bill in load_json(BILLS, []):
        if bill.get(field) == value:
            return bill
    return None


_state_lock = asyncio.Lock()


async def update_bill(bill):
    """Serialized read-modify-write: concurrent ballots must not clobber."""
    async with _state_lock:
        bills = load_json(BILLS, [])
        for i, b in enumerate(bills):
            if b["no"] == bill["no"]:
                bills[i] = bill
                break
        save_json(BILLS, bills)


async def next_bill_number():
    """Monotonic, never derived from list length."""
    async with _state_lock:
        state = load_json(STATE, {})
        seed = max((b["no"] for b in load_json(BILLS, [])), default=0)
        number = max(state.get("bill_counter", 0), seed) + 1
        state["bill_counter"] = number
        save_json(STATE, state)
        return number


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




# ---------- the chamber ----------

def chamber_overwrites(guild):
    return {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        cooperative_role(guild): discord.PermissionOverwrite(view_channel=True),
    }


async def hidden_overwrites(guild):
    owner = guild.owner or await guild.fetch_member(guild.owner_id)
    return {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        cooperative_role(guild): discord.PermissionOverwrite(view_channel=False),
        owner: discord.PermissionOverwrite(view_channel=True, send_messages=False),
    }


def bill_name(number, title, prefix="", limit=100):
    """Proposal-<number>-<as much of the title as fits>, with optional emoji
    prefix, within Discord's 100-char channel name limit."""
    stem = f"Proposal-{number}"
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
        topic=f"Debate chamber for Proposal No. {number}: {title}",
        overwrites=ow,
    )
    voice = await guild.create_voice_channel(
        bill_name(number, title, "🔊 "), category=category, overwrites=ow
    )
    return category, text, voice


# ---------- notes ----------

NOTES_PROMPT = (
    "-# Have a case to make? File your position below: named or "
    "anonymous, one slot of each, editable until the vote closes."
)


def render_note(kind, display, text):
    who = "Anonymous" if kind == "anon" else display
    return f"📝 **{who}**\n{text}"


class NoteModal(discord.ui.Modal):
    def __init__(self, bill, kind, existing):
        label = "Anonymous note" if kind == "anon" else "Note under your name"
        super().__init__(title=f"{label}: Proposal {bill['no']}"[:45])
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
            return await interaction.followup.send("This vote has closed.", ephemeral=True)
        if interaction.user.id == bill["author_id"]:
            return await interaction.followup.send(
                "The author already had their say: it is called the proposal.", ephemeral=True
            )
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
        await update_bill(bill)
        await interaction.followup.send(
            "Noted. You can edit it until the vote closes.", ephemeral=True
        )


class NotesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _open(self, interaction, kind):
        bill = bill_by("notes_message_id", interaction.message.id)
        if bill is None or bill["status"] != "on_floor":
            return await interaction.response.send_message(
                "This vote has closed.", ephemeral=True
            )
        # Arguing about a proposal is part of deciding it, so the notes are
        # gated exactly like the ballot rather than one notch looser.
        if not may_vote(bill, interaction.user):
            return await interaction.response.send_message(
                refusal_for(bill), ephemeral=True
            )
        if interaction.user.id == bill["author_id"]:
            return await interaction.response.send_message(
                "The author already had their say: it is called the proposal. "
                "The notes belong to the cooperative.",
                ephemeral=True,
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

def vote_tier(bill):
    """Removals sit a tier up. Everything else is a plain majority, which is
    what people already expect a vote to mean."""
    return bill.get("tier") or ("fundamental" if bill.get("kind") == "kick" else "normal")


def is_blind(bill):
    """Votes about a person never show a running count. You should not have
    to watch a tally climb toward throwing somebody out, and nobody arriving
    should be able to work out their own margin. Votes about things show
    everything: hiding the count on a channel rename is just ceremony."""
    return bill.get("kind") in ("invite", "kick")


def vote_state(guild, bill):
    """Everything the ballot needs to describe itself right now."""
    ballots = bill.get("ballots", {})
    exclude = {bill["target_id"]} if bill.get("target_id") else set()
    roll = roster.active(guild, in_cooperative, exclude=exclude) if guild else []
    size = len(roll)
    tier = vote_tier(bill)
    yes = sum(1 for v in ballots.values() if v == "yes")
    no = sum(1 for v in ballots.values() if v == "no")
    abstain = sum(1 for v in ballots.values() if v == "abstain")
    return {
        "size": size,
        "tier": tier,
        "need": roster.required(size, tier),
        "yes": yes,
        "no": no,
        "abstain": abstain,
        "voted": len(ballots),
        "waiting": max(size - len(ballots), 0),
    }


def bar(done, total, width=10):
    total = max(total, 1)
    filled = max(0, min(width, round(width * done / total)))
    return "█" * filled + "░" * (width - filled)


def ballot_content(guild, bill):
    """The live face of a vote. Rewritten on every ballot cast, so the
    progress toward the threshold is visible the whole way rather than
    arriving as a surprise at close."""
    st = vote_state(guild, bill)
    chamber = guild.get_channel(bill.get("chamber_text_id")) if guild else None
    where = f" · debate in {chamber.mention}" if chamber else ""
    try:
        ends = datetime.fromisoformat(bill["ends_at"])
        clock = f" · closes <t:{int(ends.timestamp())}:R>"
    except (KeyError, ValueError):
        clock = ""

    head = f"**Proposal No. {bill['no']}: {bill['title']}**{where}{clock}"
    if is_blind(bill):
        body = (
            f"🗳️ `{bar(st['voted'], st['size'])}` **{st['voted']} of "
            f"{st['size']} voted** · needs **{st['need']} yes**\n"
            f"-# No running count on a vote about a person."
        )
    else:
        body = (
            f"✅ `{bar(st['yes'], st['need'])}` **{st['yes']} of {st['need']} "
            f"yes needed**\n"
            f"❌ {st['no']}  ·  ⬜ {st['waiting']} yet to vote"
            + (f"  ·  🤍 {st['abstain']}" if st["abstain"] else "")
        )
    return (
        f"{head}\n\n{body}\n\n"
        f"-# Change or retract any time. Nobody ever sees how you voted."
    )


async def refresh_ballot(guild, bill):
    """Repaint the ballot message. Best-effort: a vote that cannot redraw
    is still a valid vote, so a failure here never blocks one."""
    if guild is None or bill.get("options") or bill.get("status") != "on_floor":
        return
    floor = room(guild, "votes")
    if floor is None or not bill.get("ballot_message_id"):
        return
    try:
        msg = await floor.fetch_message(bill["ballot_message_id"])
        await msg.edit(content=ballot_content(guild, bill))
    except discord.HTTPException as e:
        log.warning(f"could not repaint ballot for bill {bill['no']}: {e!r}")


def vote_settled(st):
    """Whether a vote's result can still change.

    Passing is settled the instant enough yes votes exist: the threshold is a
    share of the roster, not of turnout, so once it is met no later ballot can
    take it back. Failing waits for everyone, because a no can still become a
    yes while the vote is open -- an unreachable threshold is only genuinely
    unreachable once nobody is left to change their mind.

    An empty roster is never settled: that means we cannot see who is here,
    not that nobody is.
    """
    if st["size"] <= 0:
        return False
    return st["yes"] >= st["need"] or st["voted"] >= st["size"]


async def maybe_autoclose(guild, bill):
    """End a vote the moment vote_settled says it is over."""
    if bill.get("status") != "on_floor" or bill.get("options"):
        return False
    if not vote_settled(vote_state(guild, bill)):
        return False
    bill["closed_early"] = "settled"
    await close_bill(guild, bill)
    if bill.get("status") != "on_floor":
        await post_closing_report(guild, bill)
    return True


async def cast_ballot(interaction, choice):
    """Record one ballot, or retract it when choice is None. Shared by
    every ballot shape so they cannot drift apart on who may vote."""
    bill = bill_by("ballot_message_id", interaction.message.id)
    if bill is None or bill["status"] != "on_floor":
        return await interaction.response.send_message(
            "This vote has closed.", ephemeral=True
        )
    # Eligibility is decided here, from the proposal, on every single press.
    if not may_vote(bill, interaction.user):
        return await interaction.response.send_message(
            refusal_for(bill), ephemeral=True
        )
    if bill.get("kind") == "kick" and interaction.user.id == bill.get("target_id"):
        return await interaction.response.send_message(
            "You are the question, not the jury. The chamber is yours "
            "for the whole window; the ballot is not.",
            ephemeral=True,
        )
    ballots = bill.setdefault("ballots", {})
    uid = str(interaction.user.id)
    if choice is None:
        if uid in ballots:
            del ballots[uid]
            await update_bill(bill)
            await interaction.response.send_message(
                "Ballot retracted.", ephemeral=True
            )
            return await refresh_ballot(interaction.guild, bill)
        return await interaction.response.send_message(
            "You have no ballot to retract.", ephemeral=True
        )
    ballots[uid] = choice
    await update_bill(bill)
    note = (
        "Counted as present and undecided; it goes to neither side."
        if choice == "abstain"
        else "You can change it until the vote closes."
    )
    st = vote_state(interaction.guild, bill)
    if is_blind(bill):
        standing = f"{st['voted']} of {st['size']} have voted."
    else:
        left = max(st["need"] - st["yes"], 0)
        standing = (
            "That carries it."
            if left == 0
            else f"{left} more yes {'vote' if left == 1 else 'votes'} carries it."
        )
    await interaction.response.send_message(
        f"Your ballot: **{choice}**. {note} {standing} "
        f"Nobody, including the author, will ever see how you voted.",
        ephemeral=True,
    )
    await refresh_ballot(interaction.guild, bill)
    await maybe_autoclose(interaction.guild, bill)


class BallotView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _vote(self, interaction, choice):
        await cast_ballot(interaction, choice)

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


class MemberBallotView(discord.ui.View):
    """The ballot for admitting someone. Three choices instead of two,
    because in a house this small "I do not know them well enough to say"
    is an honest answer, and the two-button ballot made it look like
    absence. It goes to neither side: passage is still yes against no,
    which is what the standing orders say."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Yes", emoji="✅",
        style=discord.ButtonStyle.success, custom_id="clerk:member_yes",
    )
    async def yes(self, interaction, button):
        await cast_ballot(interaction, "yes")

    @discord.ui.button(
        label="No", emoji="❌",
        style=discord.ButtonStyle.danger, custom_id="clerk:member_no",
    )
    async def no(self, interaction, button):
        await cast_ballot(interaction, "no")

    @discord.ui.button(
        label="Abstain", emoji="🤍",
        style=discord.ButtonStyle.secondary, custom_id="clerk:member_abstain",
    )
    async def abstain(self, interaction, button):
        await cast_ballot(interaction, "abstain")

    @discord.ui.button(
        label="Retract", style=discord.ButtonStyle.secondary,
        custom_id="clerk:member_retract",
    )
    async def retract(self, interaction, button):
        await cast_ballot(interaction, None)


MULTI_MAX = 10


class MultiBallotView(discord.ui.View):
    """Choice ballot: one button per option. A registered instance with
    dummy labels handles routing after restarts; the real labels live on
    the message itself."""

    def __init__(self, options=None):
        super().__init__(timeout=None)
        labels = options if options is not None else [f"Option {i + 1}" for i in range(MULTI_MAX)]
        for i, label in enumerate(labels[:MULTI_MAX]):
            button = discord.ui.Button(
                label=str(label)[:80],
                style=discord.ButtonStyle.primary,
                custom_id=f"clerk:opt_{i}",
                row=i // 5,
            )
            button.callback = self._make_callback(i)
            self.add_item(button)
        retract = discord.ui.Button(
            label="Retract",
            style=discord.ButtonStyle.secondary,
            custom_id="clerk:opt_retract",
            row=2,
        )
        retract.callback = self._retract
        self.add_item(retract)

    def _make_callback(self, index):
        async def callback(interaction):
            await self._vote(interaction, index)
        return callback

    async def _retract(self, interaction):
        await self._vote(interaction, None)

    async def _vote(self, interaction, index):
        bill = bill_by("ballot_message_id", interaction.message.id)
        if bill is None or bill["status"] != "on_floor" or not bill.get("options"):
            return await interaction.response.send_message(
                "This vote has closed.", ephemeral=True
            )
        # Same gate as every other ballot: from the proposal, every press.
        if not may_vote(bill, interaction.user):
            return await interaction.response.send_message(
                refusal_for(bill), ephemeral=True
            )
        ballots = bill.setdefault("ballots", {})
        uid = str(interaction.user.id)
        if index is None:
            if uid in ballots:
                del ballots[uid]
                await update_bill(bill)
                return await interaction.response.send_message(
                    "Ballot retracted.", ephemeral=True
                )
            return await interaction.response.send_message(
                "You have no ballot to retract.", ephemeral=True
            )
        if index >= len(bill["options"]):
            return await interaction.response.send_message(
                "That option is not on this ballot.", ephemeral=True
            )
        choice = bill["options"][index]
        ballots[uid] = choice
        await update_bill(bill)
        await interaction.response.send_message(
            f"Your ballot: **{choice}**. You can change it until the vote closes. "
            f"No member, including the author, will ever see how you voted.",
            ephemeral=True,
        )


def multi_ballot_content(bill, ends_at, chamber_mention):
    round_note = " (runoff)" if bill.get("round", 1) > 1 else ""
    return (
        f"**Ballot** for Proposal No. {bill['no']}{round_note}: choose one option. "
        f"Cast, change, or retract until the vote closes "
        f"<t:{int(ends_at.timestamp())}:R>. Debate in {chamber_mention}. "
        f"An option needs a majority of votes cast"
        + ("; this runoff is decided by plurality" if bill.get("round", 1) > 1 else
           "; otherwise a runoff follows with the leading options")
        + ". Results appear at close; individual votes never do."
    )


async def file_bill(guild, author, title, what, why, kind="ordinary",
                    options=None, target_id=None, floor_hours=None,
                    eligible_ids=None):
    """Shared filing pipeline for all proposal kinds. Returns the filed
    proposal,
    or None if the floor is missing. Callers own their acknowledgement,
    since Eugene also files on request in conversation, where there is
    no interaction to reply to."""
    floor = room(guild, "votes")
    if floor is None:
        return None
    number = await next_bill_number()
    ends_at = now_utc() + timedelta(hours=floor_hours or FLOOR_HOURS)

    category, text, voice = await open_chamber(guild, number, title)

    stamp = await floor.send(
        view=Card([f"## Proposal No. {number}: {title}\nSubmitted by {author.mention}"])
    )
    notes_thread = await stamp.create_thread(
        name=(bill_name(number, title) + ": notes")[:100]
    )
    notes_msg = await notes_thread.send(NOTES_PROMPT, view=NotesView())
    for label, body in (("What", what), ("Why", why)):
        for i, piece in enumerate(chunk_text(body)):
            prefix = f"### {label}\n" if i == 0 else ""
            await floor.send(prefix + piece)
    if options:
        stub = {"no": number, "options": options, "round": 1}
        ballot = await floor.send(
            multi_ballot_content(stub, ends_at, text.mention),
            view=MultiBallotView(options),
        )
    else:
        if kind == "invite":
            view = MemberBallotView()
            tail = ("Yes, no, or abstain. The tally will be sealed at close; "
                    "individual votes are never seen by anyone.")
        elif kind == "kick":
            view = BallotView()
            tail = ("The tally will be sealed at close; individual votes "
                    "are never seen by anyone.")
        else:
            view = BallotView()
            tail = "Results appear at close; individual votes never do."
        ballot = await floor.send(
            f"**Ballot** for Proposal No. {number}. Cast, change, or retract "
            f"until the vote closes <t:{int(ends_at.timestamp())}:R>. "
            f"Debate in {text.mention}. {tail}",
            view=view,
        )

    bills = load_json(BILLS, [])
    bills.append(
        {
            "no": number,
            "title": title,
            "kind": kind,
            "target_id": target_id,
            "eligible_ids": eligible_ids,
            "author_id": author.id,
            "author": author.display_name,
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
            "options": options or None,
            "round": 1,
            "ballots": {},
            "notes": {},
        }
    )
    save_json(BILLS, bills)
    # Paint the live face on straight away, so a vote shows what it needs
    # from its first second rather than only once somebody has voted.
    await refresh_ballot(guild, bills[-1])
    log.info(f"proposal filed: no. {number} ({title!r}, {kind}) by {author.display_name}")
    return bills[-1]


async def file_from_modal(interaction, **kwargs):
    """Filing from a button, with the ephemeral receipt that expects."""
    bill = await file_bill(interaction.guild, interaction.user, **kwargs)
    if bill is None:
        return await interaction.followup.send(
            "The votes channel is missing. Run build_server.py first.", ephemeral=True
        )
    floor = room(interaction.guild, "votes")
    chamber = interaction.guild.get_channel(bill["chamber_text_id"])
    await interaction.followup.send(
        f"Filed. Proposal No. {bill['no']} is open: {floor.mention}, "
        f"debate in {chamber.mention}.",
        ephemeral=True,
    )
    return bill


# ---------- submitting bills ----------

class BillModal(discord.ui.Modal, title="Make a proposal"):
    bill_title = discord.ui.TextInput(
        label="Title",
        style=discord.TextStyle.short,
        placeholder="A short name for it.",
        max_length=100,
    )
    what = discord.ui.TextInput(
        label="What",
        style=discord.TextStyle.paragraph,
        placeholder="What should change? This is the text people vote on.",
        max_length=4000,
    )
    why = discord.ui.TextInput(
        label="Why",
        style=discord.TextStyle.paragraph,
        placeholder="Your reasons. A proposal without reasons is not a proposal.",
        max_length=4000,
    )
    choices = discord.ui.TextInput(
        label="Options (empty = yes/no ballot)",
        style=discord.TextStyle.paragraph,
        placeholder="For a choice ballot: one option per line, 2 to 10 lines.",
        required=False,
        max_length=800,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        options = []
        for line in str(self.choices).splitlines():
            line = line.strip()
            if line and line not in options:
                options.append(line)
        if options and not 2 <= len(options) <= MULTI_MAX:
            return await interaction.followup.send(
                f"A choice ballot needs 2 to {MULTI_MAX} distinct options "
                f"(one per line), or none at all for a yes/no ballot.",
                ephemeral=True,
            )
        await file_from_modal(
            interaction,
            title=str(self.bill_title),
            what=str(self.what),
            why=str(self.why),
            options=options or None,
        )


class InviteModal(discord.ui.Modal, title="Propose an invitation"):
    invitee = discord.ui.TextInput(
        label="Their username",
        style=discord.TextStyle.short,
        placeholder="Who should be invited?",
        max_length=100,
    )
    discord_id = discord.ui.TextInput(
        label="Their Discord ID (optional)",
        style=discord.TextStyle.short,
        required=False,
        max_length=25,
    )
    why = discord.ui.TextInput(
        label="Why",
        style=discord.TextStyle.paragraph,
        placeholder="Why should we let them in?",
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        name = str(self.invitee).strip()
        id_part = f" (Discord ID {str(self.discord_id).strip()})" if str(self.discord_id).strip() else ""
        what = (
            f"{name}{id_part} shall be invited to {interaction.guild.name}. "
            f"If this passes, Eugene issues a single-use invite link, valid "
            f"seven days, delivered privately to the proposer."
        )
        await file_from_modal(
            interaction,
            title=f"Invitation of {name}"[:100],
            what=what,
            why=str(self.why),
            kind="invite",
        )


class KickModal(discord.ui.Modal, title="Propose a removal"):
    why = discord.ui.TextInput(
        label="Why",
        style=discord.TextStyle.paragraph,
        placeholder="Say why, properly. A proposal without reasons is not a proposal.",
        max_length=4000,
    )

    def __init__(self, target: discord.Member):
        super().__init__()
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        eligible_now = len([m for m in self.target.guild.members if in_cooperative(m) and not m.bot]) - 1
        what = (
            f"{self.target.display_name} shall be removed from "
            f"{self.target.guild.name}. "
            f"Removal requires yes from all eligible voters but two; the "
            f"subject cannot vote and keeps the whole window to plead. The "
            f"tally will never be published. Eligible voters at filing: "
            f"{eligible_now}; the threshold is computed at close."
        )
        eligible_ids = [
            m.id for m in self.target.guild.members
            if in_cooperative(m) and not m.bot and m.id != self.target.id
        ]
        await file_from_modal(
            interaction,
            title=f"Removal of {self.target.display_name}"[:100],
            what=what,
            why=str(self.why),
            kind="kick",
            target_id=self.target.id,
            floor_hours=72.0,
            eligible_ids=eligible_ids,
        )


class KickTargetView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.select = discord.ui.UserSelect(placeholder="Who is this about?")
        self.select.callback = self.pick
        self.add_item(self.select)

    async def pick(self, interaction: discord.Interaction):
        target = self.select.values[0]
        guild = interaction.guild
        member = guild.get_member(target.id)
        if member is None or not in_cooperative(member):
            return await interaction.response.send_message(
                "They are not in the cooperative; there is nothing to remove.", ephemeral=True
            )
        if member.bot:
            return await interaction.response.send_message(
                "The machines are not subject to removal.", ephemeral=True
            )
        if member.id == guild.owner_id:
            return await interaction.response.send_message(
                "The platform will not allow the owner to be kicked. "
                "Ownership is a matter for elections, not removals.",
                ephemeral=True,
            )
        if member.id == interaction.user.id:
            return await interaction.response.send_message(
                "You may simply leave; the door works in both directions.",
                ephemeral=True,
            )
        for b in load_json(BILLS, []):
            if (
                b.get("kind") == "kick"
                and b.get("target_id") == member.id
                and b.get("status") == "on_floor"
            ):
                return await interaction.response.send_message(
                    "They are already up for a vote.", ephemeral=True
                )
        await interaction.response.send_modal(KickModal(member))


class SubmitBillView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def _keyed(self, interaction):
        return in_cooperative(interaction.user)

    @discord.ui.button(
        label="Make a proposal",
        emoji="🖋️",
        style=discord.ButtonStyle.primary,
        custom_id="clerk:bill",
    )
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._keyed(interaction):
            return await interaction.response.send_message(
                NOT_INSIDE, ephemeral=True
            )
        await interaction.response.send_modal(BillModal())

    @discord.ui.button(
        label="Propose an invite",
        emoji="💌",
        style=discord.ButtonStyle.secondary,
        custom_id="clerk:bill_invite",
    )
    async def invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._keyed(interaction):
            return await interaction.response.send_message(
                "That one is the cooperative's.", ephemeral=True
            )
        await interaction.response.send_modal(InviteModal())

    @discord.ui.button(
        label="Propose a removal",
        emoji="🚪",
        style=discord.ButtonStyle.secondary,
        custom_id="clerk:bill_kick",
    )
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._keyed(interaction):
            return await interaction.response.send_message(
                "That one is the cooperative's.", ephemeral=True
            )
        await interaction.response.send_message(
            "Removal is the cooperative's heaviest instrument: a 72-hour window, "
            "and it passes only if all eligible voters but two say yes.",
            view=KickTargetView(),
            ephemeral=True,
        )


# ---------- closing the floor ----------

async def publish_act(guild, bill, decided=None):
    record = room(guild, "decisions")
    if record is None:
        return ""
    acts = load_json(ACTS, [])
    act_no = len(acts) + 1
    acts.append(
        {
            "act": act_no,
            "bill": bill["no"],
            "title": bill["title"],
            "author": bill["author"],
            "what": bill["what"],
            "decided": decided,
            "tally": bill.get("tally"),
            "passed_at": bill["closed_at"],
        }
    )
    save_json(ACTS, acts)
    bill["act"] = act_no
    await record.send(view=Card([f"## Decision {act_no}: {bill['title']}"]))
    for piece in chunk_text(bill["what"]):
        await record.send(piece)
    segments = []
    if decided:
        segments.append(f"### Decided\n{decided}")
    segments.append(
        f"-# From Proposal No. {bill['no']} by {bill['author']}. "
        f"Passed with {bill['tally_line']}."
    )
    await record.send(view=Card(segments))
    return f"\n-# Recorded as Decision {act_no} in the record."


async def finalize_bill(guild, bill, passed, tally_line, decided=None):
    """Common closing: result card, published if passed, seal the notes
    thread, close the ballot, archive the chamber."""
    bill["status"] = "passed" if passed else "failed"
    bill["closed_at"] = now_utc().isoformat()
    # the tally is all the record needs from here; individual ballots are
    # destroyed at close so a closed vote cannot be reconstructed by anyone
    bill.pop("ballots", None)
    # people-bills (invite, kick) never publish numbers: a barely-admitted
    # member should never learn the margin
    secret = bill.get("kind") in ("invite", "kick")
    bill["tally_line"] = "a sealed tally" if secret else tally_line
    shown = "The tally is sealed." if secret else tally_line
    floor = room(guild, "votes")

    act_line = await publish_act(guild, bill, decided) if passed else ""

    if floor:
        if decided:
            headline = (
                f"## Proposal No. {bill['no']}: {bill['title']}\n"
                f"**Decided: {decided}**\n{shown}{act_line}"
            )
        else:
            verdict = "Passed" if passed else "Failed"
            headline = (
                f"## Proposal No. {bill['no']}: {bill['title']}\n"
                f"**{verdict}**  {shown}{act_line}"
            )
        await floor.send(view=Card([headline]))
        try:
            ballot_msg = await floor.fetch_message(bill["ballot_message_id"])
            await ballot_msg.edit(
                content=f"**Ballot closed** for Proposal No. {bill['no']}. {shown}",
                view=None,
            )
        except discord.HTTPException:
            pass
        thread_id = bill.get("notes_thread_id")
        if thread_id:
            try:
                thread = guild.get_thread(thread_id) or await guild.fetch_channel(thread_id)
                count = sum(len(s) for s in bill.get("notes", {}).values())
                await thread.send(
                    f"*Vote closed. {count} note(s) on record.*"
                    if count
                    else "*Vote closed. No notes were filed.*"
                )
                await thread.edit(archived=True, locked=True)
            except discord.HTTPException as e:
                log.warning(f"sealing notes thread failed for bill {bill['no']}: {e!r}")

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
        await voice.delete(reason="Vote closed; voice is never recorded")
    category = guild.get_channel(bill["chamber_category_id"])
    if category and not category.channels:
        await category.delete(reason="Vote closed")

    await update_bill(bill)
    log.info(f"bill closed: no. {bill['no']} {bill['status']} ({tally_line})")


# Acts whose text sounds structural: the clerk cannot build these yet, so
# the report says so plainly rather than letting a passed Act look done.
STRUCTURAL_WORDS = (
    "channel", "category", "role", "permission", "rename", "topic",
    "voice", "vc", "archive", "server_config",
)


def closing_report(bill):
    """What Eugene has already done, and what is left for human hands.
    Deterministic on purpose: Clarence relays this, he does not invent it."""
    passed = bill["status"] == "passed"
    kind = bill.get("kind", "ordinary")
    sealed = kind in ("invite", "kick")

    done = [
        "The vote is closed and the ballots are destroyed; only the tally survives.",
        "The notes are sealed with the proposal, and the chamber is archived.",
    ]
    outstanding = []

    if passed:
        if bill.get("act"):
            done.append(f"Published as Decision {bill['act']} in the record.")
        if kind == "invite":
            done.append("A single-use invite link has gone privately to the proposer.")
            outstanding.append(
                "The proposer sends the link on. It is good for one use and "
                "seven days, and nobody else can issue another without a new proposal."
            )
        elif kind == "kick":
            done.append("The removal is carried out and the role withdrawn.")
        else:
            haystack = f"{bill.get('title', '')} {bill.get('what', '')}".lower()
            if any(word in haystack for word in STRUCTURAL_WORDS):
                outstanding.append(
                    "This decision changes the shape of the server, which Eugene "
                    "cannot do himself yet. Someone edits server_config.yaml, "
                    "runs build_server.py, and deploys."
                )
            else:
                outstanding.append(
                    "Nothing in the server has changed by itself. This decision is "
                    "on the record and wants human hands to carry out."
                )
    else:
        outstanding.append(
            "Nothing to do. Anyone may file it again, reworded, whenever "
            "they like."
        )

    return {
        "bill": bill["no"],
        "title": bill.get("title", ""),
        "ruling": "passed" if passed else "failed",
        "tally": "sealed" if sealed else bill.get("tally_line", ""),
        "act": bill.get("act"),
        "done": done,
        "outstanding": outstanding,
    }


async def post_closing_report(guild, bill):
    """The standing item after every close, so a passed decision never looks
    finished when it is not."""
    floor = room(guild, "votes")
    if floor is None:
        return closing_report(bill)
    report = closing_report(bill)
    segments = ["### Done\n" + "\n".join(f"- {line}" for line in report["done"])]
    if report["outstanding"]:
        segments.append(
            "### Still wanted\n"
            + "\n".join(f"- {line}" for line in report["outstanding"])
        )
    await floor.send(view=Card(segments))
    return report


async def execute_invite(guild, bill):
    """A passed invite proposal: single-use 7-day link, DMed to the proposer."""
    doorway = invite_channel(guild)
    proposer = guild.get_member(bill["author_id"])
    if doorway is None or proposer is None:
        return await health_log(
            guild,
            f"⚠️ Proposal No. {bill['no']} passed but the invite could not be "
            f"issued (nowhere to invite them to, or the proposer has left).",
        )
    invite = await doorway.create_invite(
        max_uses=1, max_age=604800, unique=True,
        reason=f"Invitation: Proposal No. {bill['no']}",
    )
    try:
        await proposer.send(
            f"The cooperative has approved your invitation (Proposal No. {bill['no']}). "
            f"One link, single use, seven days: {invite.url}"
        )
    except discord.HTTPException:
        desk = room(guild, "proposals")
        if desk:
            await desk.send(
                f"{proposer.mention}: your invitation passed, but your DMs "
                f"are closed and Eugene cannot deliver the link. Open "
                f"them and ask at the desk."
            )
    await health_log(guild, f"⚖️ Invite issued under Proposal No. {bill['no']}.")


async def execute_kick(guild, bill):
    """A passed removal proposal: sealed farewell, role withdrawn, removal."""
    member = guild.get_member(bill.get("target_id"))
    if member is None:
        return await health_log(
            guild, f"Proposal No. {bill['no']}: the subject had already left."
        )
    try:
        await member.send(
            f"{guild.name} has voted for your removal. The "
            f"tally is sealed and will remain so. Eugene wishes you well."
        )
    except discord.HTTPException:
        pass
    role = cooperative_role(guild)
    if role and role in member.roles:
        await member.remove_roles(role, reason=f"Removal: Proposal No. {bill['no']}")
    await member.kick(reason=f"Removal: Proposal No. {bill['no']}")
    await health_log(guild, f"⚖️ Removal executed under Proposal No. {bill['no']}.")


async def close_bill(guild, bill):
    if bill.get("options"):
        return await close_multi(guild, bill)
    ballots = bill.get("ballots", {})
    yes = sum(1 for v in ballots.values() if v == "yes")
    no = sum(1 for v in ballots.values() if v == "no")
    abstain = sum(1 for v in ballots.values() if v == "abstain")
    bill["tally"] = {"yes": yes, "no": no}
    if bill.get("kind") == "invite":
        # recorded, and deliberately not counted: the standing orders
        # decide passage on yes against no, and call abstention a
        # delegation rather than a veto
        bill["tally"]["abstain"] = abstain

    if bill.get("kind") == "kick":
        # eligibility is snapshotted at filing and intersected with current
        # keyholders, so a shrinking house cannot lower the bar mid-floor,
        # and a cold member cache cannot either
        snapshot = set(bill.get("eligible_ids") or [])
        current = {
            m.id for m in guild.members
            if in_cooperative(m) and not m.bot and m.id != bill.get("target_id")
        }
        eligible = (snapshot & current) if snapshot else current
        required = max(len(eligible) - 2, KICK_MIN_YES)
        bill["threshold"] = {"eligible": len(eligible), "required": required}
        passed = yes >= required
        await finalize_bill(guild, bill, passed, f"✅ {yes} / ❌ {no}")
        if passed:
            await execute_kick(guild, bill)
        return

    st = vote_state(guild, bill)
    line = f"✅ {yes} / ❌ {no}"
    if bill.get("kind") == "invite":
        line += f" / 🤍 {abstain}"
    if st["size"] > 0:
        bill["threshold"] = {
            "roster": st["size"], "required": st["need"], "tier": st["tier"],
        }
        passed = yes >= st["need"]
        line += f" · needed {st['need']} of {st['size']}"
    else:
        # A cold member cache would otherwise read as an empty roster and
        # fail everything. Falling back to the old rule is wrong slowly;
        # failing every vote because we cannot see who is here is wrong fast.
        passed = yes > no
    await finalize_bill(guild, bill, passed, line)
    if passed and bill.get("kind") == "invite":
        await execute_invite(guild, bill)


async def close_multi(guild, bill):
    """Choice ballots: round 1 needs a strict majority of votes cast;
    otherwise a runoff opens with the leading options, decided by
    plurality. Ties in the runoff fail; the status quo never has to
    defend itself."""
    options = bill["options"]
    counts = {o: 0 for o in options}
    for v in bill.get("ballots", {}).values():
        if v in counts:
            counts[v] += 1
    total = sum(counts.values())
    leader = max(counts.values()) if counts else 0
    leaders = [o for o, n in counts.items() if n == leader]
    tally_line = " / ".join(f"{o}: {n}" for o, n in counts.items())
    bill["tally"] = counts
    final_round = bill.get("round", 1) > 1

    if total > 0 and leader * 2 > total:
        return await finalize_bill(guild, bill, True, tally_line, decided=leaders[0])
    if final_round:
        if leader > 0 and len(leaders) == 1:
            return await finalize_bill(guild, bill, True, tally_line, decided=leaders[0])
        return await finalize_bill(
            guild, bill, False, tally_line + " (tie; the status quo prevails)"
        )
    if total == 0:
        return await finalize_bill(guild, bill, False, "no votes cast")

    # runoff: keep the leading options (top two counts, ties included)
    distinct = sorted(set(counts.values()), reverse=True)
    cutoff = distinct[1] if len(distinct) > 1 else distinct[0]
    finalists = [o for o in options if counts[o] >= cutoff]
    bill["round"] = bill.get("round", 1) + 1
    bill["options"] = finalists
    bill["ballots"] = {}
    ends = now_utc() + timedelta(hours=FLOOR_HOURS)
    bill["ends_at"] = ends.isoformat()
    # A runoff is a fresh vote wearing the same number: it keeps its filing
    # date, so anything measuring how far through the window we are has to
    # measure from here instead.
    bill["round_opened_at"] = now_utc().isoformat()

    floor = room(guild, "votes")
    if floor:
        try:
            old = await floor.fetch_message(bill["ballot_message_id"])
            await old.edit(
                content=f"**Round 1 closed** for Proposal No. {bill['no']}: "
                f"no majority. {tally_line}",
                view=None,
            )
        except discord.HTTPException:
            pass
        chamber = guild.get_channel(bill["chamber_text_id"])
        mention = chamber.mention if chamber else "the chamber"
        await floor.send(
            view=Card([
                f"## Proposal No. {bill['no']}: {bill['title']}: runoff\n"
                f"No option won a majority ({tally_line}). The vote "
                f"reopens with the leading options; regroup around what "
                f"can win."
            ])
        )
        ballot = await floor.send(
            multi_ballot_content(bill, ends, mention),
            view=MultiBallotView(finalists),
        )
        bill["ballot_message_id"] = ballot.id

    await update_bill(bill)
    log.info(f"runoff opened: proposal no. {bill['no']} ({tally_line})")


@tasks.loop(seconds=60)
async def check_floor():
    guild = home_guild()
    if guild is None:
        return
    for bill in load_json(BILLS, []):
        if bill.get("status") != "on_floor" or "ends_at" not in bill:
            continue
        if datetime.fromisoformat(bill["ends_at"]) <= now_utc():
            try:
                await close_bill(guild, bill)
                # a choice ballot may have opened a runoff instead of closing
                if bill.get("status") != "on_floor":
                    await post_closing_report(guild, bill)
            except Exception as e:
                log.error(f"failed to close bill {bill['no']}: {e!r}")
                await health_log(
                    guild, f"⚠️ Failed to close Proposal No. {bill['no']}: `{e!r}`"
                )


# ---------- what Eugene says first ----------
# His one proactive loop. duties.py works out what is due and remembers what
# has already been said; everything here is the saying of it. No line below
# consults the model, so none of this costs a server anything, and none of it
# can be talked into saying something it should not.

DUTY_MINUTES = 15


def nudge_roll(guild, bill):
    """Who a vote is counted against: exactly the roll the ballot itself
    uses, so nobody is ever nudged about a vote they could not cast."""
    exclude = {bill["target_id"]} if bill.get("target_id") else set()
    return roster.active(guild, in_cooperative, exclude=exclude)


def ballot_link(guild, bill):
    floor = room(guild, "votes")
    if floor is None or not bill.get("ballot_message_id"):
        return ""
    return (
        f"\nhttps://discord.com/channels/{guild.id}/{floor.id}/"
        f"{bill['ballot_message_id']}"
    )


def nudge_text(guild, bill):
    """One short line, and never a hint at how anybody voted.

    A vote about a person shows turnout and nothing else, exactly as its
    ballot does; anything else shows the distance left to run, which is the
    part that makes the nudge worth sending at all.
    """
    st = vote_state(guild, bill)
    if is_blind(bill):
        standing = f"{st['voted']} of {st['size']} have voted."
    else:
        left = max(st["need"] - st["yes"], 0)
        standing = (
            "It already has what it needs."
            if left == 0
            else f"{left} more yes {'vote' if left == 1 else 'votes'} carries it."
        )
    try:
        closes = f" Closes <t:{int(datetime.fromisoformat(bill['ends_at']).timestamp())}:R>."
    except (KeyError, ValueError):
        closes = ""
    return (
        f"**Proposal No. {bill['no']}: {bill['title']}**\n"
        f"You have not voted, and silence counts against it. {standing}"
        f"{closes}{ballot_link(guild, bill)}\n"
        f"-# Tell me to stop nudging and I will."
    )


async def send_nudges(guild, silent=False):
    """A quiet word to whoever has not voted, once per vote, halfway through.

    Sent privately on purpose: who has and has not voted is not something to
    put in a channel, and a nudge in public is a shaming.
    """
    bills = load_json(BILLS, [])
    for bill, user_id in duties.nudges_due(bills, lambda b: nudge_roll(guild, b)):
        member = guild.get_member(user_id)
        if member is not None and not silent:
            try:
                await member.send(nudge_text(guild, bill))
                log.info(f"nudged {member.display_name} about proposal {bill['no']}")
            except discord.HTTPException as e:
                log.info(f"could not nudge {member.display_name}: {e!r}")
        # Written down either way. A door that will not open is not one to
        # keep knocking on every quarter of an hour.
        duties.mark_said(duties.nudge_key(bill, user_id))


AWAY_GONE = (
    "A fortnight quiet, so I have taken you off the roster for now. Nobody "
    "thinks anything of it and nothing you did is undone — it only means "
    "votes stop counting your silence as a no. Say anything here and you are "
    "straight back on."
)

AWAY_BACK = "You are back on the roster. Votes count you again."


async def tell_away(guild, silent=False):
    """The rules of procedure promise that Eugene marks people Away and tells
    them he did. He was doing the first half."""
    members = [m for m in guild.members if not m.bot and in_cooperative(m)]
    gone, back, quiet_now = duties.away_changes(members, roster.away_reason)
    told = [(m, AWAY_GONE) for m in gone] + [(m, AWAY_BACK) for m in back]
    if not silent:
        for member, line in told:
            if duties.muted(member.id):
                continue
            try:
                await member.send(line)
            except discord.HTTPException as e:
                log.info(f"could not tell {member.display_name} about the roster: {e!r}")
    if told:
        log.info(f"roster: {len(gone)} gone quiet, {len(back)} back")
    duties.record_quiet(quiet_now)


def outstanding_content(items, limit=1900):
    if not items:
        return (
            "### Nothing outstanding\n"
            "Every decision on the record has been carried out."
        )
    head = [
        "### Decided, and not yet done",
        "-# A decision on the record is not a thing that has happened. "
        "Tell me when one of these is carried out and it comes off the list.",
        "",
    ]
    lines, shown = list(head), 0
    for item in items:
        act = f"Decision {item['act']}" if item.get("act") else f"Proposal No. {item['no']}"
        block = [f"**{act}: {item['title']}**"] + [f"- {w}" for w in item["wants"]]
        # Whole entries only. A list cut off mid-sentence reads as a bug, and
        # "and N more" is honest about what is missing.
        if len("\n".join(lines + block)) > limit - 40:
            lines.append(f"-# …and {len(items) - shown} more.")
            break
        lines.extend(block)
        shown += 1
    return "\n".join(lines)


async def update_outstanding(guild, silent=False):
    """Keep one standing list of what still wants human hands, current at all
    times, rather than a closing report that scrolls away in an hour.

    Edited in place rather than reposted: the whole value of the thing is
    that it is always right, and nagging weekly in a channel would spend the
    goodwill that makes anyone read it. `silent` withholds only the weekly
    line; the list itself is furniture and goes up whenever it is asked for.
    """
    channel = room(guild, "decisions")
    if channel is None:
        return []
    items = duties.outstanding(load_json(BILLS, []), closing_report)
    content = outstanding_content(items)
    state = load_json(STATE, {})
    message = None
    if state.get("outstanding_message_id"):
        try:
            message = await channel.fetch_message(state["outstanding_message_id"])
        except discord.NotFound:
            message = None
        except discord.HTTPException as e:
            log.warning(f"could not read the outstanding list: {e!r}")
            return items
    if message is None:
        message = await channel.send(content)
        try:
            await message.pin(reason="What is decided and not yet done")
        except discord.HTTPException:
            pass
        # Re-read rather than reuse: the send and the pin above are awaits,
        # and something else may have written to the state file in between.
        state = load_json(STATE, {})
        state["outstanding_message_id"] = message.id
        save_json(STATE, state)
    elif message.content != content:
        await message.edit(content=content)
    # The list is silent; once a week, if it is not empty, one line points at
    # it. That is the whole of the chasing.
    if items and not silent and duties.chase_due():
        duties.mark_chased()
        await channel.send(
            f"-# {len(items)} decision(s) still want doing. The list is pinned."
        )
    return items


@tasks.loop(minutes=DUTY_MINUTES)
async def duty_loop():
    guild = home_guild()
    if guild is None:
        return
    # The first round against a fresh ledger only takes stock. Everything
    # already true when this was switched on is history, not news, and a
    # server should not be woken up by a fortnight of it at once.
    settling = duties.opening_pass()
    for duty in (send_nudges, tell_away, update_outstanding):
        try:
            await duty(guild, silent=settling)
        except Exception as e:
            # One duty failing must not take the other two down with it.
            log.error(f"duty {duty.__name__} failed: {e!r}")
            await health_log(guild, f"⚠️ `{duty.__name__}` failed: `{e!r}`")
    if settling:
        duties.mark_started()
        log.info("duty ledger opened; from here on he speaks up")


# ---------- custom roles (purely aesthetic) ----------

def role_registry():
    return load_json(ROLES, {})


def created_by(user_id):
    return [rid for rid, meta in role_registry().items() if meta["creator_id"] == user_id]


def at_create_cap(user_id):
    """The one reading of the creation cap. The cap blocks new roles only;
    anyone who made several before it came down keeps them."""
    return len(created_by(user_id)) >= ROLE_CREATE_MAX


def role_cap_line(subject="You"):
    """The refusal when someone is at that cap, said the same way by the
    modal, the panel and Eugene's own hands -- and still reading properly
    if the cap ever moves off one."""
    if ROLE_CREATE_MAX == 1:
        return f"{subject} already made a role. It has to go before another can exist."
    return f"{subject} already made {ROLE_CREATE_MAX} roles. One has to go to make room."


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
    """Raises ValueError on anything that is not a hex colour."""
    value = (value or "").strip().lstrip("#")
    try:
        return discord.Colour.from_str(f"#{value}")
    except Exception as e:
        raise ValueError(str(e)) from e


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
        if not name or name.lower() in {COOPERATIVE.lower(), "clerk", "eugene", "@everyone", "@here"}:
            return await interaction.response.send_message("Pick another name.", ephemeral=True)
        try:
            colour = parse_colour(str(self.color))
        except Exception:
            return await interaction.response.send_message(
                "That is not a hex color. Try something like #ff9d2e.", ephemeral=True
            )
        if at_create_cap(interaction.user.id):
            return await interaction.response.send_message(
                role_cap_line(), ephemeral=True
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
        await ensure_color_stack(interaction.guild)
        await update_wardrobe(interaction.guild)


async def ensure_color_stack(guild):
    """Custom colour roles outrank the booster role, so a member's chosen
    colour wins over Nitro pink. Requires the Clerk role to sit above
    Server Booster; the bot cannot promote itself there."""
    registry = role_registry()
    customs = sorted(
        (r for r in guild.roles if str(r.id) in registry),
        key=lambda r: r.position,
    )
    if not customs:
        return
    booster = guild.premium_subscriber_role
    floor_pos = booster.position if booster else 0
    if all(r.position > floor_pos for r in customs):
        return
    ceiling = guild.me.top_role.position
    if ceiling <= floor_pos + len(customs):
        return log.warning(
            "cannot lift colour roles above boosters: drag Eugene's role "
            "above Server Booster in Server Settings > Roles"
        )
    positions = {r: floor_pos + 1 + i for i, r in enumerate(customs)}
    try:
        await guild.edit_role_positions(
            positions, reason="Colour roles outrank boosts"
        )
        log.info(f"lifted {len(customs)} colour roles above the booster role")
    except discord.HTTPException as e:
        log.warning(f"colour stack lift failed: {e!r}")


def owns_role(user_id, role):
    """The single authority check for custom-role actions. Every edit,
    delete, and reorder path goes through this; the dropdown filter is a
    convenience, never the control."""
    if role is None:
        return False
    meta = role_registry().get(str(role.id))
    return meta is not None and meta.get("creator_id") == user_id


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
        if not owns_role(interaction.user.id, role):
            return await interaction.response.send_message(
                "That role is not yours to edit.", ephemeral=True
            )
        try:
            colour = parse_colour(str(self.color))
        except Exception:
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
        """Re-checks ownership on every action, not just at menu build."""
        role = interaction.guild.get_role(self.role_id)
        return role if owns_role(interaction.user.id, role) else None

    @discord.ui.button(label="Rename / recolor", style=discord.ButtonStyle.primary)
    async def edit(self, interaction, button):
        role = self._role(interaction)
        if role is None:
            return await interaction.response.send_message(
                "That role is gone, or it is not yours.", ephemeral=True
            )
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
            return await interaction.response.send_message(
                "That role is gone, or it is not yours.", ephemeral=True
            )
        registry = role_registry()
        del registry[str(role.id)]
        save_json(ROLES, registry)
        await role.delete(reason=f"Deleted by creator {interaction.user.display_name}")
        await interaction.response.edit_message(content="Deleted.", view=None)
        await update_wardrobe(interaction.guild)

    async def _shift(self, interaction, direction):
        role = self._role(interaction)
        if role is None:
            return await interaction.response.send_message(
                "That role is gone, or it is not yours.", ephemeral=True
            )
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
        if not owns_role(interaction.user.id, role):
            return await interaction.response.send_message(
                "That role is gone, or it is not yours.", ephemeral=True
            )
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
        if not in_cooperative(interaction.user):
            return await interaction.response.send_message(
                "That one is for people who are in.", ephemeral=True
            )
        if at_create_cap(interaction.user.id):
            return await interaction.response.send_message(
                role_cap_line(), ephemeral=True
            )
        await interaction.response.send_modal(RoleCreateModal())

    @discord.ui.button(
        label="Wear / shed", emoji="🧥",
        style=discord.ButtonStyle.secondary, custom_id="clerk:role_wear",
    )
    async def wear(self, interaction, button):
        if not in_cooperative(interaction.user):
            return await interaction.response.send_message(
                "That one is for people who are in.", ephemeral=True
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

    @discord.ui.button(
        label="Nerd mode", emoji="🩺",
        style=discord.ButtonStyle.secondary, custom_id="clerk:nerd_toggle",
    )
    async def nerd_mode(self, interaction, button):
        if not in_cooperative(interaction.user):
            return await interaction.response.send_message(
                "That one is for people who are in.", ephemeral=True
            )
        role = discord.utils.get(interaction.guild.roles, name=NERD)
        if role is None:
            return await interaction.response.send_message(
                "The nerd role is missing. Run build_server.py first.", ephemeral=True
            )
        if role in interaction.user.roles:
            await interaction.user.remove_roles(role, reason="Nerd mode off")
            return await interaction.response.send_message(
                "Nerd mode off. Eugene's vitals are once again none of your business.",
                ephemeral=True,
            )
        await interaction.user.add_roles(role, reason="Nerd mode on")
        channel = health_channel(interaction.guild)
        where = f" {channel.mention} awaits." if channel else ""
        await interaction.response.send_message(
            f"Nerd mode on.{where}", ephemeral=True
        )


# ---------- colour-role actions the clerk may perform for a member ----------
# These act AS the invoker, inside the invoker's own powers: identical
# limits to the buttons in #roles. No privilege escalation is possible,
# so they need no Act; every rule below is enforced here in Python, never
# by the model.

BANNED_ROLE_NAMES = {"@everyone", "@here"}


def _protected_names():
    return BANNED_ROLE_NAMES | {COOPERATIVE.lower(), MEMBER.lower(), NERD, "clerk", "eugene"}


def _find_custom_role(guild, needle):
    registry = role_registry()
    needle = (needle or "").strip().lower()
    for role in guild.roles:
        if str(role.id) in registry and role.name.lower() == needle:
            return role
    for role in guild.roles:
        if str(role.id) in registry and needle and needle in role.name.lower():
            return role
    return None


async def act_list_colors(guild, member, args):
    registry = role_registry()
    out = []
    for r in custom_stack(guild):
        creator = guild.get_member(registry[str(r.id)]["creator_id"])
        out.append(
            {
                "name": r.name,
                "colour": f"#{r.colour.value:06x}",
                "wearers": len([m for m in r.members if not m.bot]),
                "made_by": creator.display_name if creator else "someone departed",
                "yours": creator.id == member.id if creator else False,
            }
        )
    return json.dumps(out)


async def act_create_color(guild, member, args):
    name = (args.get("name") or "").strip()[:100]
    if not name or name.lower() in _protected_names():
        return "That name is not available."
    if at_create_cap(member.id):
        return role_cap_line(member.display_name)
    try:
        colour = parse_colour(args.get("color", ""))
    except ValueError:
        return "That is not a hex colour."
    role = await guild.create_role(
        name=name, colour=colour, permissions=discord.Permissions.none(),
        mentionable=False, hoist=False,
        reason=f"Colour role for {member.display_name}, via Eugene",
    )
    registry = role_registry()
    registry[str(role.id)] = {"creator_id": member.id}
    save_json(ROLES, registry)
    worn = ""
    if len(worn_custom(member)) < ROLE_WEAR_MAX:
        await member.add_roles(role, reason="Creator wears their creation")
        worn = " and is wearing it"
    await ensure_color_stack(guild)
    await update_wardrobe(guild)
    return f"Created {role.name} ({args.get('color')}) for {member.display_name}{worn}."


async def act_edit_color(guild, member, args):
    role = _find_custom_role(guild, args.get("role"))
    if not owns_role(member.id, role):
        return "No such colour role of theirs; only the creator may change one."
    kwargs = {}
    new_name = (args.get("name") or "").strip()
    if new_name and new_name.lower() not in _protected_names():
        kwargs["name"] = new_name[:100]
    if args.get("color"):
        try:
            kwargs["colour"] = parse_colour(args["color"])
        except ValueError:
            return "That is not a hex colour."
    if not kwargs:
        return "Nothing to change."
    await role.edit(**kwargs)
    await update_wardrobe(guild)
    return f"Updated {role.name}."


async def act_delete_color(guild, member, args):
    role = _find_custom_role(guild, args.get("role"))
    if not owns_role(member.id, role):
        return "No such colour role of theirs; only the creator may delete one."
    registry = role_registry()
    registry.pop(str(role.id), None)
    save_json(ROLES, registry)
    name = role.name
    await role.delete(reason=f"Deleted by creator {member.display_name}, via Eugene")
    await update_wardrobe(guild)
    return f"Deleted {name}."


async def act_wear_color(guild, member, args):
    role = _find_custom_role(guild, args.get("role"))
    if role is None:
        return "No colour role by that name."
    if role in member.roles:
        return f"{member.display_name} is already wearing {role.name}."
    if len(worn_custom(member)) >= ROLE_WEAR_MAX:
        return f"{member.display_name} is wearing {ROLE_WEAR_MAX} already; one must come off."
    await member.add_roles(role, reason="Worn via Eugene")
    await update_wardrobe(guild)
    return f"{member.display_name} is now wearing {role.name}."


async def act_shed_color(guild, member, args):
    role = _find_custom_role(guild, args.get("role"))
    if role is None or role not in member.roles:
        return "They are not wearing that one."
    await member.remove_roles(role, reason="Shed via Eugene")
    await update_wardrobe(guild)
    return f"{member.display_name} took off {role.name}."


COLOR_ACTIONS = {
    "list_color_roles": act_list_colors,
    "create_color_role": act_create_color,
    "edit_color_role": act_edit_color,
    "delete_color_role": act_delete_color,
    "wear_color_role": act_wear_color,
    "shed_color_role": act_shed_color,
}


# ---------- filing bills in conversation ----------
# Same powers the buttons in #submit-a-bill already give every citizen,
# reached by asking instead of clicking. The bill is filed in the asker's
# name, because the standing orders say laws have public authors, and
# Clarence has no vote on what he files.

# There is no cap on how much may be open at once. There was one, and it
# only ever bound the people who asked Eugene to file for them: the buttons
# in #submit-a-bill never consulted it, so anybody who wanted round it just
# clicked instead. A rule that stops the polite route and not the other one
# is not a rule, it is a tax on asking nicely. If a busy floor turns out to
# be a real problem it is the cooperative's to fix, by proposal, and the
# fix belongs where every route passes: file_bill.


def _clean(value, limit):
    return str(value or "").strip()[:limit]


async def act_propose_bill(guild, invoker, args):
    title = _clean(args.get("title"), 100)
    what = _clean(args.get("what"), 4000)
    why = _clean(args.get("why"), 4000)
    if not (title and what and why):
        return json.dumps({"error": "a proposal needs a title, a what, and a why"})
    bill = await file_bill(guild, invoker, title=title, what=what, why=why)
    if bill is None:
        return json.dumps({"error": "the votes channel is missing"})
    return json.dumps({"filed": bill["no"], "title": title,
                       "author": bill["author"], "closes_at": bill["ends_at"]})


async def act_propose_member(guild, invoker, args):
    name = _clean(args.get("name"), 80)
    discord_id = _clean(args.get("discord_id"), 25)
    why = _clean(args.get("why"), 4000)
    if not name:
        return json.dumps({"error": "name the person being proposed"})
    if not why:
        return json.dumps({"error": "a proposal without reasons is not a proposal"})
    if discord_id and not discord_id.isdigit():
        return json.dumps({"error": "a Discord ID is digits only, or leave it out"})
    tail = f" (Discord ID {discord_id})" if discord_id else ""
    what = (
        f"{name}{tail} shall be invited to {guild.name}. If this passes, "
        f"Eugene issues a single-use invite link, valid seven days, "
        f"delivered privately to the proposer."
    )
    bill = await file_bill(
        guild, invoker, title=f"Invitation of {name}"[:100],
        what=what, why=why, kind="invite",
    )
    if bill is None:
        return json.dumps({"error": "the votes channel is missing"})
    return json.dumps({"filed": bill["no"], "proposed": name,
                       "author": bill["author"], "closes_at": bill["ends_at"],
                       "ballot": "yes, no, or abstain; anonymous; tally sealed at close"})


# A proposal can be closed before its window runs out, but not the instant
# it is filed: an author could otherwise file, vote for themselves, close,
# and have a decision before anyone saw it exist. It must have run a
# quarter of its own window first, which scales with floor_hours and so
# stays workable on a three-minute sandbox setting.
EARLY_CLOSE_FRACTION = 0.25


async def close_floor(guild, invoker, bill_no):
    """Call time on a vote, in the invoker's name. Returns a plain dict:
    an {"error": ...} refusal, or the closing report. Both the conversation
    and `/close` come through here, so the early-close guard is written
    once and neither route can drift from the other."""
    bill = bill_by("no", bill_no)
    if bill is None:
        return {"error": f"no Proposal No. {bill_no} on record"}
    if bill.get("status") != "on_floor":
        return {"error": f"Proposal No. {bill_no} closed already",
                "ruling": bill.get("status"), "act": bill.get("act")}

    submitted = datetime.fromisoformat(bill["submitted_at"])
    ends = datetime.fromisoformat(bill["ends_at"])
    window = (ends - submitted).total_seconds()
    elapsed = (now_utc() - submitted).total_seconds()
    if window > 0 and elapsed < window * EARLY_CLOSE_FRACTION:
        opens = submitted + timedelta(seconds=window * EARLY_CLOSE_FRACTION)
        return {"error": "too early: not everyone has had a fair look at "
                         "this one yet",
                "closable_from": opens.isoformat()}

    floor = room(guild, "votes")
    if floor:
        await floor.send(
            view=Card([
                f"### Time called on Proposal No. {bill['no']}\n"
                f"{invoker.mention} has closed the vote early. Anyone who "
                f"had not voted no longer can."
            ])
        )
    await close_bill(guild, bill)
    if bill.get("status") == "on_floor":
        return {"bill": bill["no"],
                "ruling": "runoff",
                "note": "No option had a majority, so the vote reopened with "
                        "the leading options instead of closing.",
                "closes_at": bill["ends_at"]}
    report = await post_closing_report(guild, bill)
    report["closed_early_by"] = invoker.display_name
    log.info(f"proposal no. {bill['no']} closed early by {invoker.display_name}")
    return report


async def act_close_floor(guild, invoker, args):
    try:
        bill_no = int(args.get("bill_no"))
    except (TypeError, ValueError):
        return json.dumps({"error": "which proposal? give the number"})
    return json.dumps(await close_floor(guild, invoker, bill_no))


BILL_ACTIONS = {
    "propose_bill": act_propose_bill,
    "propose_member": act_propose_member,
    "close_floor": act_close_floor,
}


# ---------- the other end of the two things Eugene starts ----------
# A nudge you cannot stop is a nuisance, and a standing list of unfinished
# work with no way to say "done" is a nag. Both of these are member-tier for
# the same reason the colour tools are: neither hands anybody a power they
# did not already have. Saying a decision has been carried out is a claim on
# the record, made in public, under the name of whoever made it.

async def act_set_nudges(guild, invoker, args):
    on = args.get("on")
    if isinstance(on, str):
        on = on.strip().lower() not in ("false", "no", "off", "0")
    on = True if on is None else bool(on)
    duties.set_muted(invoker.id, on=not on)
    log.info(f"nudges {'on' if on else 'off'} for {invoker.display_name}")
    return json.dumps(
        {
            "nudges": "on" if on else "off",
            "note": (
                "Reminders about votes they have not cast are back on."
                if on
                else "No more reminders. Votes and their clocks are unchanged; "
                     "they simply will not hear from you about them."
            ),
        }
    )


async def act_mark_carried_out(guild, invoker, args):
    try:
        bill_no = int(args.get("bill_no"))
    except (TypeError, ValueError):
        return json.dumps({"error": "which decision? give the number"})
    bill = bill_by("no", bill_no)
    if bill is None:
        return json.dumps({"error": f"no Proposal No. {bill_no} on record"})
    if bill.get("status") != "passed":
        return json.dumps(
            {"error": f"Proposal No. {bill_no} did not pass, so there is "
                      f"nothing to carry out",
             "status": bill.get("status")}
        )
    if bill.get("carried_out"):
        return json.dumps(
            {"note": "already marked done", "by": bill["carried_out"].get("by")}
        )
    bill["carried_out"] = {
        "by": invoker.display_name,
        "by_id": invoker.id,
        "at": now_utc().isoformat(),
    }
    await update_bill(bill)
    log.info(f"decision {bill_no} marked carried out by {invoker.display_name}")
    return json.dumps(
        {
            "bill": bill_no,
            "title": bill.get("title", ""),
            "act": bill.get("act"),
            "note": "Off the outstanding list, recorded against their name. "
                    "Anyone can see who said so.",
        }
    )


DUTY_ACTIONS = {
    "set_nudges": act_set_nudges,
    "mark_carried_out": act_mark_carried_out,
}

# ---------- installing Eugene in a server ----------
# This is for whoever runs the place, not for everyone else: shaping the
# server and paying for Eugene's thinking are not put to a vote. A server brings its own brain key, typed into a modal and
# answered ephemerally, so it reaches no channel, no log, and no other
# screen; the store keeps it 0600 and shows only its last four digits.



def is_steward(interaction):
    """Whoever could already reshape this server by hand. The command is
    hidden from everyone else, but hiding is not a boundary, so it is
    checked again here."""
    user = interaction.user
    return isinstance(user, discord.Member) and (
        user.guild_permissions.administrator
        or user.id == interaction.guild.owner_id
    )


def brain_lines(guild):
    """The state of every annex, in a form safe to put on a screen."""
    on_duty = brain.provider_name(guild.id)
    if on_duty is None:
        return [
            "Brain: **dormant**, no key. `/setup brain` wakes him.",
            f"-# He speaks through {' or '.join(providers.label(n) for n in providers.NAMES)}. "
            f"Any one will do, or several.",
        ]
    lines = []
    for name in providers.NAMES:
        key = settings.brain_key(guild.id, name)
        if not key:
            lines.append(f"- {providers.label(name)}: no key")
            continue
        mark = "**on duty**" if name == on_duty else "standing by"
        who = settings.get(guild.id, f"{name}_key_set_by")
        lines.append(
            f"- {providers.label(name)}: {mark}, `{brain.model_name(guild.id, name)}`, "
            f"key {settings.fingerprint(key)}"
            + (f", set by {who}" if who else "")
        )
    spent = brain.spend_usd(guild.id)
    lines.insert(0, f"Brain: **awake** through {providers.label(on_duty)}")
    lines.append(
        f"Spent this month: ${spent:.2f} of ${settings.budget_usd(guild.id):.0f}"
    )
    if not intents.message_content:
        lines.append(
            "⚠️ This host runs without the Message Content intent, so he "
            "cannot hear a word. See the README on `CLERK_MESSAGE_CONTENT`."
        )
    return lines


ANNEX_CHOICES = [
    app_commands.Choice(name=providers.label(name), value=name)
    for name in providers.NAMES
]

setup_group = app_commands.Group(
    name="setup",
    description="Install and configure Eugene in this server",
    guild_only=True,
    default_permissions=discord.Permissions(administrator=True),
)


@setup_group.command(name="status", description="What is configured here")
async def setup_status(interaction: discord.Interaction):
    if not is_steward(interaction):
        return await interaction.response.send_message(
            "That one is for whoever runs the place, sorry.", ephemeral=True
        )
    guild = interaction.guild
    bindings.prune(guild)
    # An empty cooperative is the failure that looks like a working install:
    # every room bound, every permission granted, and Eugene refusing
    # everybody who speaks to him. Say it at the top, where it cannot be
    # missed, rather than leaving it to be inferred from a role binding.
    coop = cooperative_role(guild)
    inside = (
        [m for m in guild.members if not m.bot and coop in m.roles]
        if coop is not None else []
    )
    lines = [
        f"## Eugene in {guild.name}",
        "Governance: "
        + ("**ready**" if bindings.ready(guild)
           else "**dormant** — run `/setup start`"),
        f"Cooperative: **{len(inside)}** "
        + ("person" if len(inside) == 1 else "people")
        + (" — nobody can propose, vote, or talk to me until somebody is in. "
           "`/setup start` puts you there." if not inside else ""),
        "This place, in his words: *" + brain.house_description(guild.id) + "*"
        + ("" if settings.get(guild.id, "house") else " (the default; "
           "`/setup house` says what this server is actually for)"),
        "",
        bindings.summary(guild),
        "",
        *brain_lines(guild),
        f"Vote window: {FLOOR_HOURS:g}h (backstop only)",
        f"-# Commit `{COMMIT}`.",
    ]
    lacking = builder.missing_permissions(guild)
    if lacking:
        lines.insert(1, "⚠️ Missing permissions: " + ", ".join(lacking))
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------- binding rooms and roles to what already exists ----------
# Eugene is pointed at a server; he does not reshape one. Everything is stored
# by id, so renaming a channel afterwards costs nothing.

class RoomSelect(discord.ui.ChannelSelect):
    def __init__(self, key, label):
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder=f"{key} — {label}",
            min_values=0,
            max_values=1,
        )
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        if not is_steward(interaction):
            return await interaction.response.send_message(
                "That one is for whoever runs the place, sorry.", ephemeral=True
            )
        if not self.values:
            bindings.bind_channel(interaction.guild.id, self.key, None)
            return await interaction.response.send_message(
                f"`{self.key}` unbound.", ephemeral=True
            )
        picked = self.values[0]
        bindings.bind_channel(interaction.guild.id, self.key, picked.id)
        log.info(f"bound room {self.key} -> #{picked.name} ({picked.id})")
        await interaction.response.send_message(
            f"`{self.key}` → {picked.mention}. Rename it whenever you like; "
            f"I go by id.",
            ephemeral=True,
        )


class RoleBindSelect(discord.ui.RoleSelect):
    def __init__(self, key, label):
        super().__init__(
            placeholder=f"{key} — {label}", min_values=0, max_values=1
        )
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        if not is_steward(interaction):
            return await interaction.response.send_message(
                "That one is for whoever runs the place, sorry.", ephemeral=True
            )
        if not self.values:
            bindings.bind_role(interaction.guild.id, self.key, None)
            return await interaction.response.send_message(
                f"`{self.key}` unbound.", ephemeral=True
            )
        picked = self.values[0]
        bindings.bind_role(interaction.guild.id, self.key, picked.id)
        log.info(f"bound role {self.key} -> {picked.name} ({picked.id})")
        await interaction.response.send_message(
            f"`{self.key}` → {picked.mention}.", ephemeral=True
        )


class RoomsView(discord.ui.View):
    """Up to five selects per message, which is exactly the number of jobs."""

    def __init__(self, keys):
        super().__init__(timeout=300)
        for key in keys:
            self.add_item(RoomSelect(key, bindings.ROOMS[key]))


class RolesBindView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        for key, label in bindings.ROLES.items():
            self.add_item(RoleBindSelect(key, label))


@setup_group.command(
    name="rooms", description="Point Eugene at the channels he should use"
)
async def setup_rooms(interaction: discord.Interaction):
    if not is_steward(interaction):
        return await interaction.response.send_message(
            "That one is for whoever runs the place, sorry.", ephemeral=True
        )
    keys = list(bindings.ROOMS)
    pages = [keys[i:i + 5] for i in range(0, len(keys), 5)]
    await interaction.response.send_message(
        "**Which channel does which job?**\n"
        "Pick an existing channel for each. Leave one unset to switch that "
        "feature off; clear a menu to unbind it.\n"
        "-# Stored by id, so renaming a channel later changes nothing.\n\n"
        + bindings.summary(interaction.guild),
        view=RoomsView(pages[0]),
        ephemeral=True,
    )
    for page in pages[1:]:
        await interaction.followup.send(view=RoomsView(page), ephemeral=True)


@setup_group.command(
    name="roles", description="Tell Eugene which role votes and which does not"
)
async def setup_roles(interaction: discord.Interaction):
    if not is_steward(interaction):
        return await interaction.response.send_message(
            "That one is for whoever runs the place, sorry.", ephemeral=True
        )
    await interaction.response.send_message(
        "**Which role is which?**\n"
        "`cooperative` votes. `member` is in the room without a vote — leave "
        "it empty if everyone here votes.\n\n"
        + bindings.summary(interaction.guild),
        view=RolesBindView(),
        ephemeral=True,
    )


class BrainKeyModal(discord.ui.Modal):
    """The key never leaves this modal for anywhere public: modal input is
    private to the person typing, every reply below is ephemeral, and only
    the last four digits are ever shown or logged."""

    key = discord.ui.TextInput(
        label="API key",
        style=discord.TextStyle.short,
        min_length=16,
        max_length=200,
    )
    model = discord.ui.TextInput(
        label="Model (blank for the default)",
        style=discord.TextStyle.short,
        required=False,
        max_length=80,
    )

    def __init__(self, guild, annex):
        # The annex is named in the title and the placeholders rather than
        # the field label, which discord.py has deprecated setting late.
        super().__init__(title=f"Wake Eugene through {providers.label(annex)}")
        self.guild = guild
        self.annex = annex
        self.key.placeholder = providers.PROVIDERS[annex].key_hint
        self.model.placeholder = providers.default_model(annex)
        current = settings.model(guild.id, annex)
        if current:
            self.model.default = current

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        key = str(self.key).strip()
        model = str(self.model).strip() or providers.default_model(self.annex)
        problem = await brain.validate_key(self.annex, key, model)
        if problem:
            return await interaction.followup.send(
                f"Nothing saved. {problem}", ephemeral=True
            )
        settings.set_brain_key(
            self.guild.id, self.annex, key,
            by=interaction.user.display_name,
            at=now_utc().isoformat(),
        )
        settings.set_model(self.guild.id, self.annex, model)
        brain.forget_client(self.guild.id)
        log.info(
            f"{self.annex} key set for guild {self.guild.id} by "
            f"{interaction.user.display_name} ({settings.fingerprint(key)})"
        )
        others = [
            n for n in settings.keyed_providers(self.guild.id, providers.NAMES)
            if n != self.annex
        ]
        await interaction.followup.send(
            f"Done. He is awake through {providers.label(self.annex)} on "
            f"`{model}`, and the key ({settings.fingerprint(key)}) stays here.\n"
            + (
                f"-# {' and '.join(providers.label(n) for n in others)} "
                f"still on file; `/setup use` switches between them.\n"
                if others else ""
            )
            + "-# Mention him to talk.",
            ephemeral=True,
        )
        await update_health(self.guild)


@setup_group.command(
    name="brain", description="Give Eugene an AI key"
)
@app_commands.describe(annex="Whose thinking to rent")
@app_commands.choices(annex=ANNEX_CHOICES)
async def setup_brain(interaction: discord.Interaction,
                      annex: app_commands.Choice[str]):
    if not is_steward(interaction):
        return await interaction.response.send_message(
            "That one is for whoever runs the place, sorry.", ephemeral=True
        )
    await interaction.response.send_modal(
        BrainKeyModal(interaction.guild, annex.value)
    )


@setup_group.command(
    name="use", description="Switch which AI does the talking here"
)
@app_commands.describe(annex="Which one speaks from now on")
@app_commands.choices(annex=ANNEX_CHOICES)
async def setup_use(interaction: discord.Interaction,
                    annex: app_commands.Choice[str]):
    if not is_steward(interaction):
        return await interaction.response.send_message(
            "That one is for whoever runs the place, sorry.", ephemeral=True
        )
    guild = interaction.guild
    if not settings.brain_key(guild.id, annex.value):
        return await interaction.response.send_message(
            f"There is no {annex.name} key here yet. "
            f"`/setup brain annex:{annex.name}` sets one.",
            ephemeral=True,
        )
    settings.set_provider(guild.id, annex.value)
    brain.forget_client(guild.id)
    log.info(f"guild {guild.id} switched to {annex.value}")
    await interaction.response.send_message(
        f"{annex.name} it is, on `{brain.model_name(guild.id)}`. "
        f"The other key stays on file.",
        ephemeral=True,
    )
    await update_health(guild)


@setup_group.command(
    name="forget-brain", description="Remove one of this server's AI keys"
)
@app_commands.describe(annex="Whose key to forget")
@app_commands.choices(annex=ANNEX_CHOICES)
async def setup_forget_brain(interaction: discord.Interaction,
                             annex: app_commands.Choice[str]):
    if not is_steward(interaction):
        return await interaction.response.send_message(
            "That one is for whoever runs the place, sorry.", ephemeral=True
        )
    guild = interaction.guild
    if not settings.brain_key(guild.id, annex.value):
        return await interaction.response.send_message(
            f"There is no {annex.name} key here to forget.", ephemeral=True
        )
    settings.clear_brain_key(guild.id, annex.value)
    brain.forget_client(guild.id)
    log.info(f"{annex.value} key cleared for guild {guild.id}")
    still = brain.provider_name(guild.id)
    await interaction.response.send_message(
        f"Forgotten. " + (
            f"He carries on through {providers.label(still)}."
            if still else
            "He keeps the records and holds the door as before, and says nothing."
        ),
        ephemeral=True,
    )
    await update_health(guild)


# What `/setup create` will make, when a job has no channel yet. Suggested
# names only: if the server already has a channel by that name it is bound as
# it stands, never renamed, never re-topiced, never moved. Governance rooms
# only -- a bot that turns up in somebody's server and makes them a memes
# channel has overstepped, and this list is where that promise is kept.
NEW_ROOMS = {
    "proposals": ("propose", "Say what should change, and why.", True),
    "votes": ("votes", "Proposals up for a vote. Ballots are anonymous.", True),
    "decisions": ("decisions", "Every decision, numbered. The permanent record.", False),
    "polls": ("polls", "Polls open to everyone. Advisory.", False),
}


async def make_missing_rooms(guild):
    """Create and bind a channel for every job that has none. Strictly
    additive: it never renames, moves, re-topics, re-permissions, reorders
    or deletes anything that already exists -- including channels it finds
    by name, which are adopted exactly as they are. Nothing outside
    NEW_ROOMS is touched at all.

    Returns (made, bound, skipped) as lists of display lines. Shared by
    `/setup create` and `/setup start`, so the careful one and the quick
    one cannot drift apart.
    """
    coop = cooperative_role(guild)
    made, bound, skipped = [], [], []
    for key, (suggested, topic, coop_only) in NEW_ROOMS.items():
        if bindings.channel(guild, key) is not None:
            skipped.append(f"`{key}` already bound")
            continue
        existing = discord.utils.get(guild.text_channels, name=suggested)
        if existing is not None:
            bindings.bind_channel(guild.id, key, existing.id)
            bound.append(f"`{key}` → {existing.mention} (adopted, unchanged)")
            continue
        overwrites = None
        if coop_only and coop is not None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                coop: discord.PermissionOverwrite(view_channel=True),
            }
        try:
            made_channel = await guild.create_text_channel(
                suggested, topic=topic, overwrites=overwrites,
                reason="Created by /setup",
            )
        except discord.HTTPException as e:
            skipped.append(f"`{key}` could not be created: {e!r}")
            continue
        bindings.bind_channel(guild.id, key, made_channel.id)
        made.append(f"`{key}` → {made_channel.mention}")
    return made, bound, skipped


@setup_group.command(
    name="start",
    description="First run: make the roles and rooms, and put you in the cooperative",
)
async def setup_start(interaction: discord.Interaction):
    """The one command a fresh server should need.

    Everything here was already possible -- create a role by hand, bind it
    with `/setup roles`, give it to yourself, then `/setup create` -- but the
    first step of that chain happened outside Discord's slash commands, so a
    new server arrived with nobody in the cooperative and no way in. Eugene
    then refused every single person, including the one who installed him,
    and named no route out of it. That is the bug this fixes: one command
    that leaves somebody inside.

    Additive throughout. It adopts an existing Cooperative role rather than
    making a second one, and never takes a role away from anybody.
    """
    if not is_steward(interaction):
        return await interaction.response.send_message(
            "That one is for whoever runs the place, sorry.", ephemeral=True
        )
    guild = interaction.guild
    lacking = builder.missing_permissions(guild)
    if lacking:
        return await interaction.response.send_message(
            "I cannot do this yet — I am missing: " + ", ".join(lacking)
            + ".\nGrant them, drag my role to the top of the list, and "
            "run this again.",
            ephemeral=True,
        )
    await interaction.response.defer(ephemeral=True, thinking=True)

    steps = []

    def say(line):
        steps.append(f"- {line}")

    # Roles first: the rooms below want the cooperative role to hide behind.
    coop = await builder.ensure_cooperative(guild, say)
    member = await builder.ensure_member(guild, say)
    bindings.bind_role(guild.id, "cooperative", coop.id)
    bindings.bind_role(guild.id, "member", member.id)
    say(f"bound `cooperative` → {coop.mention} and `member` → {member.mention}")

    # The whole point: somebody ends up inside.
    you = interaction.user
    if coop in you.roles:
        say("you were already in the cooperative")
    else:
        try:
            await you.add_roles(coop, reason="/setup start: the first member")
            say(f"gave you {coop.mention}")
        except discord.HTTPException as e:
            say(f"could not give you {coop.mention}: {e!r} — "
                "check my role sits above it in the list")

    made, bound, skipped = await make_missing_rooms(guild)
    for line in made:
        say(f"created {line}")
    for line in bound:
        say(f"found and bound {line}")

    lines = [
        "## Eugene is set up",
        "Nothing that already existed was renamed, moved or re-permissioned.",
        "",
        *steps,
        "",
        "**What is left:**",
    ]
    left = []
    if not brain.provider_name(guild.id):
        left.append("`/setup brain` — give him an AI key so he can talk.")
    left.append(
        "`/setup grant` — hand the cooperative to anyone else who should have "
        "a vote. After that, the ordinary way in is `/invite`, which is a vote."
    )
    if not settings.get(guild.id, "house"):
        left.append(
            "`/setup house` — one line on what this server is for. He reads "
            "the name off Discord, but not the point of the place."
        )
    if bindings.missing_rooms(guild):
        left.append(
            "`/setup rooms` — the optional jobs (welcome, health, chat) are "
            "still unbound. Point them at channels you already have."
        )
    if skipped:
        left.append("Left alone: " + ", ".join(skipped) + ".")
    lines += [f"- {item}" for item in left]
    await interaction.followup.send("\n".join(lines)[:1990], ephemeral=True)


@setup_group.command(
    name="house", description="Tell Eugene in one line what this server is for"
)
@app_commands.describe(
    description="e.g. a book club that argues about endings. Blank to reset."
)
async def setup_house(interaction: discord.Interaction, description: str = ""):
    """The server's own words about itself, which Eugene is told.

    He used to be told, in as many words, that he ran a particular house
    full of friends who wanted to play games without a headache. That was
    true of one server and false everywhere else, and it is the sort of
    falsehood he repeats confidently. The server's name he reads off
    Discord; what the place is for, only its people know.

    Stored per guild and constant between edits, so it sits in the cached
    half of the prompt and costs nothing to send on every request.
    """
    if not is_steward(interaction):
        return await refuse(
            interaction, "That one is for whoever runs the place, sorry."
        )
    text = " ".join(description.split())[:300]
    settings.put(interaction.guild.id, house=text or None)
    if not text:
        return await interaction.response.send_message(
            "Reset. He will describe this place as "
            f"*{brain.DEFAULT_HOUSE}* — true of most servers, specific to "
            "none.",
            ephemeral=True,
        )
    await interaction.response.send_message(
        f"Noted. He now knows **{interaction.guild.name}** as "
        f"*{text}*.\n-# It reaches him on his next message; it does not "
        f"reach anyone else.",
        ephemeral=True,
    )


@setup_group.command(
    name="grant", description="Put someone in the cooperative, by hand"
)
@app_commands.describe(member="Who gets a vote")
async def setup_grant(interaction: discord.Interaction, member: discord.Member):
    """The bootstrap door, and only that.

    Once there is a cooperative, the way in is `/invite` and a vote -- this
    is not a shortcut around it, and the reply says so. But a server with
    nobody inside cannot hold a vote about letting somebody in, so the first
    few have to be handed over by whoever runs the place.
    """
    if not is_steward(interaction):
        return await refuse(
            interaction, "That one is for whoever runs the place, sorry."
        )
    guild = interaction.guild
    coop = cooperative_role(guild)
    if coop is None:
        return await refuse(
            interaction,
            "There is no cooperative role here yet. `/setup start` makes one.",
        )
    if member.bot:
        return await refuse(interaction, "Bots do not vote. Nor do I.")
    if coop in member.roles:
        return await refuse(
            interaction, f"{member.display_name} is already in the cooperative."
        )
    try:
        await member.add_roles(coop, reason=f"/setup grant by {interaction.user}")
    except discord.HTTPException as e:
        return await refuse(
            interaction,
            f"Could not: {e!r}. My role has to sit above {coop.mention}.",
        )
    log.info(f"{interaction.user} granted cooperative to {member} in {guild.id}")
    await interaction.response.send_message(
        f"{member.display_name} is in the cooperative. That is one more vote "
        f"the thresholds count against.\n-# This is the bootstrap door. Once "
        f"there are a few of you, `/invite` is the ordinary way in, and it is "
        f"a vote.",
        ephemeral=True,
    )


@setup_group.command(
    name="revoke", description="Take the cooperative role back off someone"
)
@app_commands.describe(member="Who loses the vote")
async def setup_revoke(interaction: discord.Interaction, member: discord.Member):
    """Undoing a `/setup grant` typo, not removing a person.

    Removal from the cooperative as a decision goes through `/remove`, a
    72-hour vote with a sealed tally. This is for the case where the wrong
    name was picked out of the menu a minute ago.
    """
    if not is_steward(interaction):
        return await refuse(
            interaction, "That one is for whoever runs the place, sorry."
        )
    coop = cooperative_role(interaction.guild)
    if coop is None or coop not in member.roles:
        return await refuse(
            interaction, f"{member.display_name} is not in the cooperative."
        )
    try:
        await member.remove_roles(coop, reason=f"/setup revoke by {interaction.user}")
    except discord.HTTPException as e:
        return await refuse(interaction, f"Could not: {e!r}")
    log.info(f"{interaction.user} revoked cooperative from {member} in "
             f"{interaction.guild.id}")
    await interaction.response.send_message(
        f"Taken back off {member.display_name}.\n-# For an actual removal, "
        f"`/remove` puts it to the cooperative rather than to you.",
        ephemeral=True,
    )


@setup_group.command(
    name="create",
    description="Create only the governance channels that do not exist yet",
)
async def setup_create(interaction: discord.Interaction):
    """The rooms half of `/setup start`, on its own. Strictly additive."""
    if not is_steward(interaction):
        return await interaction.response.send_message(
            "That one is for whoever runs the place, sorry.", ephemeral=True
        )
    guild = interaction.guild
    me = guild.me
    if me is None or not me.guild_permissions.manage_channels:
        return await interaction.response.send_message(
            "I need Manage Channels to create anything.", ephemeral=True
        )
    await interaction.response.defer(ephemeral=True, thinking=True)

    made, bound, skipped = await make_missing_rooms(guild)
    lines = ["**Nothing existing was changed.**"]
    if made:
        lines += ["", "Created:"] + [f"- {m}" for m in made]
    if bound:
        lines += ["", "Found and bound as-is:"] + [f"- {b}" for b in bound]
    if skipped:
        lines += ["", "Left alone:"] + [f"- {s}" for s in skipped]
    if cooperative_role(guild) is None:
        lines += ["", "-# No cooperative role is bound, so the private rooms "
                  "were made without one. `/setup start` makes and binds it, "
                  "then run this again."]
    await interaction.followup.send("\n".join(lines), ephemeral=True)


bot.tree.add_command(setup_group)


# ---------- the same powers, typed ----------
# Every command here is a second door onto a view or a helper that already
# exists: no new governance, and nothing here reaches the brain. Eugene only
# thinks when he is spoken to, so a member who types /propose costs the
# cooperative nothing, where asking him for the same thing in a channel costs
# a conversation. These are member-tier on purpose -- none of them hands
# anybody a power the buttons did not already give them.

def keyed_in(interaction):
    """The member gate, interaction-shaped. `in_cooperative` remains the
    only definition of who is in."""
    return (isinstance(interaction.user, discord.Member)
            and in_cooperative(interaction.user))


async def refuse(interaction, line):
    """A refusal is always the first response, which leaves the permitted
    path free to open a modal -- Discord allows that only as a first
    response, never after a deferral."""
    await interaction.response.send_message(line, ephemeral=True)


def _as_sentence(text):
    """`close_floor` answers in the register the model reads. A member
    reading the same refusal wants a sentence."""
    text = str(text).strip()
    tail = "" if text.endswith((".", "!", "?")) else "."
    return text[:1].upper() + text[1:] + tail


@bot.tree.command(name="propose", description="Say what should change, and why")
@app_commands.guild_only()
async def slash_propose(interaction: discord.Interaction):
    if not keyed_in(interaction):
        return await refuse(interaction, NOT_INSIDE)
    await interaction.response.send_modal(BillModal())


@bot.tree.command(name="invite", description="Propose that someone be let in")
@app_commands.guild_only()
async def slash_invite(interaction: discord.Interaction):
    if not keyed_in(interaction):
        return await refuse(interaction, "That one is the cooperative's.")
    await interaction.response.send_modal(InviteModal())


@bot.tree.command(name="remove", description="Propose that someone be removed")
@app_commands.guild_only()
async def slash_remove(interaction: discord.Interaction):
    if not keyed_in(interaction):
        return await refuse(interaction, "That one is the cooperative's.")
    # The weight of the thing is said before the picker, not after, so
    # nobody names a person without having read what it costs.
    await interaction.response.send_message(
        "Removal is the cooperative's heaviest instrument: a 72-hour window, "
        "and it passes only if all eligible voters but two say yes.",
        view=KickTargetView(),
        ephemeral=True,
    )


@bot.tree.command(name="close", description="Call time on a vote that has had its run")
@app_commands.describe(number="Which proposal")
@app_commands.guild_only()
async def slash_close(interaction: discord.Interaction, number: int):
    if not keyed_in(interaction):
        return await refuse(interaction, "Only the cooperative calls time on a vote.")
    # Closing archives a chamber and publishes a decision: too much work
    # for the three seconds Discord allows before it gives up on us.
    await interaction.response.defer(ephemeral=True, thinking=True)
    report = await close_floor(interaction.guild, interaction.user, number)

    if "error" in report:
        line = _as_sentence(report["error"])
        if report.get("closable_from"):
            when = datetime.fromisoformat(report["closable_from"])
            line += f" It can be closed <t:{int(when.timestamp())}:R>."
        return await interaction.followup.send(line, ephemeral=True)
    if report.get("ruling") == "runoff":
        return await interaction.followup.send(report["note"], ephemeral=True)

    lines = [f"Proposal No. {report['bill']} {report['ruling']}."]
    if report.get("act"):
        lines.append(f"It is on the record as Decision {report['act']}.")
    if report.get("tally") and report["tally"] != "sealed":
        lines.append(report["tally"])
    if report.get("outstanding"):
        lines += ["", "Still wanted:"] + [f"- {item}" for item in report["outstanding"]]
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(name="bills", description="What is open for a vote right now")
@app_commands.guild_only()
async def slash_bills(interaction: discord.Interaction):
    if not keyed_in(interaction):
        return await refuse(interaction, "The floor is the cooperative's.")
    open_bills = sorted(
        (b for b in load_json(BILLS, []) if b.get("status") == "on_floor"),
        key=lambda b: b["no"],
    )
    if not open_bills:
        return await interaction.response.send_message(
            "Nothing is open. The floor is yours.", ephemeral=True
        )
    floor = room(interaction.guild, "votes")
    lines = ["**Open for a vote**"]
    # A title can run to a hundred characters, so a busy floor is trimmed
    # rather than sent and refused by Discord for length.
    for bill in open_bills[:10]:
        ends = int(datetime.fromisoformat(bill["ends_at"]).timestamp())
        lines.append(
            f"**No. {bill['no']}: {bill['title']}** — {bill.get('author', 'someone')}, "
            f"closes <t:{ends}:R>"
        )
    rest = len(open_bills) - 10
    if rest > 0:
        lines.append(f"-# And {rest} more.")
    if floor:
        lines.append(f"-# Ballots are in {floor.mention}.")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="role", description="Make or manage your colour role")
@app_commands.guild_only()
async def slash_role(interaction: discord.Interaction):
    if not keyed_in(interaction):
        return await refuse(interaction, "That one is for people who are in.")
    # Always the whole wardrobe, never straight into the create modal. A
    # member gets one colour of their own, and someone who typed /role
    # meaning to wear a colour already on the rack must not be handed the
    # form that spends it.
    await interaction.response.send_message(
        "Your wardrobe.", view=RolesHomeView(), ephemeral=True
    )


# ---------- pinned buttons ----------

async def ensure_button_message(channel, state_key, content, view, restamp=False):
    if channel is None:
        log.warning(f"channel for {state_key} missing; button not posted")
        return
    state = load_json(STATE, {})
    msg_id = state.get(state_key)
    if msg_id:
        try:
            message = await channel.fetch_message(msg_id)
            if restamp:
                # once per boot: new buttons/wording appear after a deploy
                await message.edit(content=content, view=view)
            return
        except discord.NotFound:
            pass
    message = await channel.send(content, view=view)
    state[state_key] = message.id
    save_json(STATE, state)
    log.info(f"button posted in #{channel.name}")


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
        guild = home_guild() if ready else None
        return web.json_response(
            {
                "status": "ok",
                "commit": COMMIT,
                "clerk": str(bot.user) if ready else None,
                "guild": guild.name if guild else None,
                "ready": ready,
                "latency_ms": round(bot.latency * 1000) if ready else None,
                "open_bills": sum(1 for b in bills if b.get("status") == "on_floor"),
                "acts": len(load_json(ACTS, [])),
                "custom_roles": len(load_json(ROLES, {})),
            }
        )

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info(f"health endpoint listening on :{port}/healthz")


# ---------- lifecycle ----------

@bot.event
async def setup_hook():
    await start_web()
    toolbox.configure(
        HERE, DATA, {**COLOR_ACTIONS, **BILL_ACTIONS, **DUTY_ACTIONS},
        in_cooperative=in_cooperative,
    )
    settings.configure(DATA)
    roster.configure(DATA)
    duties.configure(DATA)
    # Upgrade in place: a host that still carries a key in its environment
    # hands it to the server it serves, once, and is never read again.
    adopted = settings.adopt_env_keys(GUILD_ID)
    if adopted:
        log.info(f"adopted host {', '.join(adopted)} key(s) into guild {GUILD_ID}")
    brain.configure(
        bot, HERE, DATA, in_cooperative, health_log, chunk_text, resolve_guild,
        role_limits={"create": ROLE_CREATE_MAX, "wear": ROLE_WEAR_MAX},
    )
    if not intents.message_content:
        log.warning(
            "running without the Message Content intent: Eugene cannot "
            "hear anything, whatever keys a server sets"
        )
    bot.add_view(SubmitBillView())
    bot.add_view(BallotView())
    bot.add_view(MemberBallotView())
    bot.add_view(NotesView())
    bot.add_view(RolesHomeView())
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)


async def ensure_furniture(guild, restamp=False):
    """Post any missing furniture. Runs at boot (restamp=True, so
    deploys refresh buttons and wording) and periodically (verify only)."""
    await ensure_button_message(
        room(guild, "proposals"),
        "bill_message_id",
        "*Say what should change, and why. Eugene files it and everyone "
        "votes. Authorship is public; rules do not have anonymous "
        "authors.*",
        SubmitBillView(),
        restamp=restamp,
    )
    await ensure_button_message(
        roles_channel(guild),
        "roles_home_id",
        "*Self-service: a role is a name and a color, nothing more. "
        "Create up to five, wear up to five, yours or anyone's.*",
        RolesHomeView(),
        restamp=restamp,
    )
    await ensure_color_stack(guild)
    await update_wardrobe(guild)
    await update_health(guild)


@tasks.loop(seconds=300)
async def furniture_loop():
    guild = home_guild()
    if guild:
        # Clear bindings whose channel or role has been deleted, so a room
        # that quietly went away shows as unbound instead of as silence.
        bindings.prune(guild)
        try:
            await ensure_furniture(guild)
        except Exception as e:
            log.error(f"furniture check failed: {e!r}")
            await health_log(guild, f"⚠️ Furniture check failed: `{e!r}`")


@bot.event
async def on_ready():
    log.info(f"on duty as {bot.user} (commit {COMMIT})")
    guild = home_guild()
    if guild:
        await ensure_furniture(guild, restamp=not getattr(bot, "_boot_announced", False))
        if not getattr(bot, "_boot_announced", False):
            bot._boot_announced = True
            state = load_json(STATE, {})
            if state.get("announced_commit") != COMMIT:
                state["announced_commit"] = COMMIT
                save_json(STATE, state)
                await health_log(guild, f"🟢 On duty. Commit `{COMMIT}`.")
    if not check_floor.is_running():
        check_floor.start()
    if not furniture_loop.is_running():
        furniture_loop.start()
    if not duty_loop.is_running():
        duty_loop.start()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    # Being around is what keeps you on the roster; nobody should have to
    # file paperwork to prove they still exist.
    if message.guild is not None:
        roster.touch(message.author.id)
    try:
        await brain.handle_message(message)
    except Exception as e:
        log.error(f"brain handler error: {e!r}")


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot or not serves(member.guild):
        return
    welcome = room(member.guild, "polls") or find_channel(member.guild, "general")
    if welcome:
        await welcome.send(f"Welcome, {member.mention}.")


if __name__ == "__main__":
    logging.getLogger("discord").setLevel(logging.WARNING)
    bot.run(TOKEN, log_level=logging.INFO)
