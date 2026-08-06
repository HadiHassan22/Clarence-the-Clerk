"""Eugene: the engine of whichever server he is in.

Nothing here names a house. The server's own name comes from `guild.name`
wherever one is needed -- proposal text, the character prompt, the health
card -- and its rooms and roles are bound by id through `/setup`. He was
written for one server and it is no longer that server; anything that
hardcodes a name is a bug, not a default.

Roles:      given by people, not earned at a button. Eugene reads them.
Proposing: say what should change (title/what/why), Eugene publishes it,
            opens a thread on it to argue in, and runs an anonymous
            ballot.
Voting:     thresholds count against the roster rather than turnout unless
            the house says otherwise, so a vote ends the moment its result
            is settled rather than when the clock runs out -- and where the
            house counts against turnout, the ending rule follows it. A
            majority carries most things; a removal wants all the eligible
            bar two. voting.floor_hours is only the backstop for a vote
            nobody finishes.
At close:   result posted, passed proposals become numbered decisions in
            the record, and the thread -- the argument and the final notes
            both -- is locked and archived where it stands. Choice ballots
            (author supplies 2-10 options)
            need a strict majority of votes cast; otherwise a runoff opens
            with the leading options, decided by plurality. They are the
            same ballot as any other -- live face, self-closing, one gate on
            who may vote -- only with more buttons. Authors cannot file
            notes on their own proposals.

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
import modules
import polls
import powers
import providers
import roster
import settings
import toolbox

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

# Both at once, and said in words. A KeyError out of the top of the file is
# a true answer to a question nobody asked: the host gets a traceback about
# os.environ, a restart policy obligingly produces ten more of it, and
# nothing anywhere says which variable, what goes in it, or where it is
# set. It costs four lines to say that instead.
if not (os.environ.get("DISCORD_TOKEN") or "").strip():
    raise SystemExit(
        "Eugene cannot start: DISCORD_TOKEN is not set.\n"
        "On a host it is the service's environment variable; on a laptop it "
        "lives in a .env file next to clerk.py. It is the bot token from the "
        "Discord developer portal."
    )
TOKEN = os.environ["DISCORD_TOKEN"].strip()
# GUILD_ID used to be required and used to be the whole design: one daemon,
# one house. It is optional now and means only "sync my commands to this
# server first", which is the difference between seeing a new command
# immediately and waiting an hour for Discord to publish it globally.
try:
    DEV_GUILD_ID = int((os.environ.get("GUILD_ID") or "").strip() or 0) or None
except ValueError:
    DEV_GUILD_ID = None

CONFIG = yaml.safe_load((HERE / "server_config.yaml").read_text())
# The one governance number that predates the settings store. It stays
# readable from the file because CONTRIBUTING tells people to wind it down
# to seconds for a sandbox run, and that has to work before a server has
# been configured at all. It is a default now, not the rule: a house that
# sets its own window with `/setup` outranks it.
CONFIG_FLOOR_HOURS = float(CONFIG.get("voting", {}).get("floor_hours", 48))

# State lives next to the code by default; on a host with a persistent
# disk (e.g. Render's /data), point CLERK_DATA_DIR there.
DATA = Path(os.environ.get("CLERK_DATA_DIR", HERE))
DATA.mkdir(parents=True, exist_ok=True)

# The record, per server. It sat at the top of the data directory from when
# there was only ever one house, which meant moving the daemon to a second
# server meant arriving with the first one's proposals and its numbering.
# `state_file` adopts the old path once, so an upgrade in place keeps its
# history instead of starting at Proposal No. 1.
def _gid(guild):
    return guild.id if hasattr(guild, "id") else int(guild)


def state_path(guild):
    return settings.state_file(_gid(guild), "clerk_state.json", legacy_root=DATA)


def bills_path(guild):
    return settings.state_file(_gid(guild), "bills.json", legacy_root=DATA)


def acts_path(guild):
    return settings.state_file(_gid(guild), "acts.json", legacy_root=DATA)


def numbers(guild=None):
    """Every number this house votes by, read fresh.

    Deliberately not cached and never bound to a module constant: a house
    that changes a threshold has changed it now, not at the next deploy,
    and every rule quoted at a member -- on a ballot, in a nudge, by the
    brain -- has to come from here or it will eventually quote a number
    that stopped being true.
    """
    return settings.voting(guild.id if guild is not None else None)

log = logging.getLogger("clerk")

COOPERATIVE = "Cooperative"  # holds a vote: whoever picked up a chore
MEMBER = "Member"            # in the room, no vote; unused while all of
                             # us are in the cooperative
BELL = "Bell"                # rung when a ballot opens, and never otherwise

# Said to somebody who is not in the cooperative. It names the way in on
# purpose: a refusal that only says no leaves a new arrival stuck, which is
# exactly how the first install went -- the person who installed Eugene was
# outside, and nothing anywhere told them how to get inside.
#
# It used to name `/invite`, and that was wrong twice over. `/invite` is the
# server's door: it ends in a link to a room the person reading this is
# already standing in. The cooperative is a different door and it has never
# been a vote -- somebody who has it hands it over.
NOT_INSIDE = (
    "Only the cooperative files proposals here: whoever has picked up a "
    "chore. Somebody who runs the place hands that over under `/setup` → "
    "Roles & votes. (`/invite` is the door into the server, and you are "
    "already through it.)"
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
# moment with /setup, so the intent is asked for up front rather
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

base_name = bindings.base_name  # one definition, shared with the builder


# What each room may be called. Lookups try the current name first and fall
# back, so a deploy works either side of the server rebuild and keeps working
# if nobody ever gets round to it.
#
# Derived from bindings.JOBS rather than written out again: the same knowledge
# in two tables is the same knowledge until somebody edits one of them, and the
# two setup routes disagreeing about what a room is called is exactly the bug
# this pair of tables caused.
CHANNEL_ALIASES = {}
for _room_name, _job in bindings.JOBS.items():
    CHANNEL_ALIASES.setdefault(_job, []).append(_room_name)
CHANNEL_ALIASES["propose"] = CHANNEL_ALIASES["proposals"]


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

    The binding set through `/setup` always wins. Falling back to a name
    match keeps a server that has not been pointed at anything yet working
    exactly as it did before, so binding is an upgrade rather than a cutover.

    A job marked `bound_only` skips the fallback. There is one, and the
    reason is written where it is declared: guessing which room a vote goes
    in is untidy if it is wrong, and guessing which room to greet arrivals
    in is talking over whatever the server already had.
    """
    if guild is None:
        return None
    found = bindings.channel(guild, key)
    if found is not None or modules.bound_only(key):
        return found
    return find_channel(guild, key)


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


# ---------- the bell ----------
# One ping, when a new ballot opens, to the people who asked for one. It
# rides on the ballot's own card and nowhere else: no card in #votes
# advertising itself, no message before the proposal and none after it.
#
# Opt-in on both sides. Somebody who wants telling picks the role up on
# `/house`; a house that would rather nothing in it was ever pinged turns
# the whole thing off under `/setup` -> Roles & votes. Neither of those is
# the default state of anybody: a fresh server has the role and nobody in
# it, so the first ballot mentions nobody at all.

RINGING = "bell"


def bell_role(guild):
    return (
        bindings.role(guild, "bell")
        or discord.utils.get(guild.roles, name=BELL)
    )


def ringing(guild):
    """Whether this house rings the bell at all.

    On out of the box and harmless there, because the role starts empty:
    nothing is pinged until somebody asks to be. The switch is for the
    house that does not want the option to exist, and it is a house-level
    answer rather than a per-person one, which is why it is not simply a
    matter of everybody dropping the role.
    """
    if guild is None:
        return False
    return bool(settings.get(guild.id, RINGING, True))


def set_ringing(guild, on):
    """A house back on the default stops being stored, like every other
    switch here, so a later change to the default reaches them."""
    settings.put(guild.id, **{RINGING: None if on else False})


def bell_for(guild):
    """The role a new ballot should ring, or None when there is nobody to
    ring it for.

    Four ways to be nobody and they all end here: the house switched the
    bell off, nobody ever made the role, nobody holds it, or there is no
    server in the question. Each of them means the card goes up exactly as
    it did before there was a bell, because a mention with nothing behind
    it is a line of blue text that then has to explain itself.
    """
    if guild is None or not ringing(guild):
        return None
    role = bell_role(guild)
    if role is None:
        return None
    holders = [m for m in getattr(role, "members", ())
               if not getattr(m, "bot", False)]
    return role if holders else None


def ring(bell=None):
    """What a card is allowed to ping: the bell, or nothing.

    Named on every send rather than left to the default, and the reason is
    not the bell. A proposal's What and Why are somebody else's words drawn
    straight onto the card, so a proposal that says `@everyone` becomes an
    `@everyone` unless the message that carries it says otherwise -- and
    Eugene has Administrator, so Discord will honour it.
    """
    return discord.AllowedMentions(
        everyone=False, users=False,
        roles=[bell] if bell is not None else False,
    )


def archive_category(guild):
    return next((c for c in guild.categories if "archive" in c.name.lower()), None)


async def health_log(guild, text):
    """An operational line for whoever runs the host.

    This used to be a message in a pinned, administrator-only #bot-health
    room, alongside a card carrying the commit, the latency and the
    month's spend. The room is gone: it was a channel, a permission dance
    and a rewritten message per boot, to say what a host's own log says
    for nothing -- and it put the bill in Discord, where the people who
    can read it are not the people who pay it.
    """
    del guild  # kept in the signature: every caller has one, and the day
               # this posts somewhere again it will want to know where
    log.info(text)
def in_cooperative(member):
    role = cooperative_role(member.guild)
    return role is not None and role in member.roles


def in_room(member):
    """Anyone who is in at all: the cooperative, or a member without a vote.
    The cooperative is a superset, so holding either is enough.

    A house that has bound no Member role is not a house with nobody in the
    room; it is a house where being in the server is the whole of what
    being in the room means, which is how ours runs and how a fresh install
    runs on its first day. Reading an unbound role as "nobody" would make
    every poll that says it is open to everyone quietly the cooperative's
    again, which is the kind of difference between what a thing says and
    what it does that this bot exists to not have.
    """
    if getattr(member, "bot", False):
        return False
    if in_cooperative(member):
        return True
    role = member_role(member.guild)
    if role is None:
        return True
    return role in member.roles


# Who a ballot is open to. Cooperative business is the default, because a
# thing that forgets to say what it is should be the closed kind, never the
# open one.
# Every vote here is the cooperative's. There used to be a second audience
# -- a poll put to the whole server, deciding nothing -- and it doubled
# almost everything below: two electorates, two quorum rules, two rooms,
# two refusals, and a `quorum` field that meant something in one and
# nothing in the other. A house that wants everybody to have a vote gives
# everybody the role, which is one decision in one place rather than a
# second kind of ballot running alongside the first.
COOPERATIVE_ONLY = "cooperative"


def belongs_to(bill):
    """The membership test a ballot admits people by.

    Still a function of the proposal rather than a constant, because
    `may_vote` and the denominator have to be the same question asked
    twice: a vote counted against a wider roster than the one allowed to
    vote in it can never pass, and one counted against a narrower roster
    passes on fewer people than it claims.
    """
    return in_cooperative


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
    return belongs_to(bill)(member)


def floor_for(guild, bill):
    """The room a vote is held in.

    Voting, and nothing else. What a vote *came to* is not voting: the
    result, the report of what it set in motion and the window to take it
    back all belong to the record, and a floor carrying four cards after
    every close is a floor nobody can find the open ballot in.
    """
    return room(guild, "votes")


def record_for(guild, bill):
    """The room a vote's outcome is kept in."""
    return room(guild, "decisions")


def outcome_for(guild, bill):
    """Where a result is announced.

    The record, always, where the house keeps one. A server that has not
    got a decisions channel falls back to the floor, because a ruling
    announced nowhere is worse than a ruling announced in the wrong room --
    but that is the fallback and not the shape.
    """
    return record_for(guild, bill) or floor_for(guild, bill)


def electorate(guild, bill):
    """Every id this vote is counted against: the people the ballot would
    let in, minus whoever is away, minus the subject of a removal."""
    if guild is None:
        return []
    exclude = {bill["target_id"]} if bill.get("target_id") else set()
    return roster.active(
        guild, belongs_to(bill), exclude=exclude,
        away_days=numbers(guild)["away_days"],
    )


def refusal_for(bill):
    return ("This one is the cooperative's to decide, and whatever it "
            "decides lands in #decisions.")


# ---------- which house ----------
# He keeps as many houses as he has been invited to. Everything that needs
# to know which one asks here rather than reading the environment, which is
# what made changing this a three-function job rather than a rewrite.

def houses():
    """Every server he keeps."""
    return list(bot.guilds)


def serves(guild):
    return guild is not None and guild in bot.guilds


def resolve_guild(message):
    """The server a message is addressed to.

    In a channel it is plain. A direct message has no guild at all, so it
    is answered for the one server both he and the writer are in -- and
    only then. Somebody in two of his houses gets no answer rather than an
    answer about whichever one happened to sort first: a private message
    that quietly picks a house is a private message that quotes the wrong
    roster at you.
    """
    if message.guild is not None:
        return message.guild if serves(message.guild) else None
    shared = [g for g in bot.guilds
              if g.get_member(message.author.id) is not None]
    return shared[0] if len(shared) == 1 else None


def bill_by(guild, field, value):
    for bill in load_json(bills_path(guild), []):
        if bill.get(field) == value:
            return bill
    return None


_state_lock = asyncio.Lock()


async def update_bill(guild, bill):
    """Serialized read-modify-write: concurrent ballots must not clobber."""
    async with _state_lock:
        path = bills_path(guild)
        bills = load_json(path, [])
        for i, b in enumerate(bills):
            if b["no"] == bill["no"]:
                bills[i] = bill
                break
        save_json(path, bills)


async def next_bill_number(guild):
    """Monotonic, never derived from list length."""
    async with _state_lock:
        path = state_path(guild)
        state = load_json(path, {})
        seed = max((b["no"] for b in load_json(bills_path(guild), [])), default=0)
        number = max(state.get("bill_counter", 0), seed) + 1
        state["bill_counter"] = number
        save_json(path, state)
        return number


# ---------- rendering ----------

class Card(discord.ui.LayoutView):
    """One gold-striped container holding text segments, and whatever
    buttons the thing it draws still has.

    Everything long-lived Eugene posts is one of these, redrawn rather than
    added to: a proposal on the floor from filing to result, and a
    proposal in the record from its ruling to the window closing on it.
    Persistent, because a deploy in the middle of either must not leave a
    card sitting there with nothing behind its buttons.
    """

    def __init__(self, segments=(), rows=()):
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_colour=ACCENT)
        for i, segment in enumerate(segments):
            if i:
                container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(segment))
        for row in rows:
            container.add_item(row)
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

def admin_only_overwrites(guild):
    """A room only administrators and the owner can read.

    There is no role in this and there must not be one. Discord's
    Administrator permission bypasses channel overwrites by itself, so
    denying everybody is the whole implementation: what is left is exactly
    the set of people who could already read anything here by hand, and
    there is nothing to hand out, forget to revoke, or copy to a friend.
    That is the same reasoning `is_admin` is written from.

    The bot is the exception, and it has to be explicit. He is the only one
    who ever posts here, and a bot without Administrator is inside
    "everybody" -- so a room denied to everyone is a room he cannot write
    the health card into. Whatever the deny says, the writer is named.
    """
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    coop = cooperative_role(guild)
    if coop is not None:
        ow[coop] = discord.PermissionOverwrite(view_channel=False)
    members = member_role(guild)
    if members is not None:
        ow[members] = discord.PermissionOverwrite(view_channel=False)
    me = getattr(guild, "me", None)
    if me is not None:
        ow[me] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_messages=True,
            read_message_history=True,
        )
    return ow


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


def chamber_of(guild, bill):
    """Where a proposal is argued about, whatever shape that is here.

    One thread, hanging off the proposal on the floor: the notes are filed
    in it and the argument happens in it. It has no permissions of its own,
    which is the point -- it inherits the floor's, so who can read the
    debate and who can read the ballot are the same question with the same
    answer, and there is no second set of overwrites to drift from the
    first.

    It was briefly two threads, one for the argument and one for the
    record, and before that a category holding a text channel and a voice
    channel. Proposals filed under either are still open, so all three are
    looked up here rather than at each of the places that want to point at
    one.
    """
    if guild is None:
        return None
    thread_id = bill.get("chamber_thread_id") or bill.get("notes_thread_id")
    if thread_id:
        get_thread = getattr(guild, "get_thread", None)
        thread = get_thread(thread_id) if get_thread else None
        return thread or guild.get_channel(thread_id)
    return guild.get_channel(bill.get("chamber_text_id"))


# ---------- notes ----------

NOTES_PROMPT = (
    "-# Argue it out here. Want your position on the record instead? File "
    "it below: named or anonymous, one slot of each, editable until the "
    "vote closes."
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
        bill = bill_by(interaction.guild, "no", self.bill_no)
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
        await update_bill(interaction.guild, bill)
        await interaction.followup.send(
            "Noted. You can edit it until the vote closes.", ephemeral=True
        )


class NotesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _open(self, interaction, kind):
        bill = bill_by(interaction.guild, "notes_message_id", interaction.message.id)
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

# The tier a kind of proposal is carried at, where it is not the ordinary
# one. A removal sits a tier up because it is somebody's standing. An
# invitation sits on a tier of its own, which starts life as the same plain
# majority it always was -- the point of the tier is that a house can price
# the door without touching what anything else costs.
KIND_TIERS = {"kick": "fundamental", "invite": "invite"}


def vote_tier(bill):
    """Removals sit a tier up, invitations on a tier of their own.
    Everything else is a plain majority, which is what people already
    expect a vote to mean."""
    return bill.get("tier") or KIND_TIERS.get(bill.get("kind"), "normal")


def is_blind(bill):
    """Votes about a person never show a running count while they are open.
    You should not have to watch a tally climb toward throwing somebody out,
    and a count that is still moving is the one people lobby against. Votes
    about things show everything: hiding the count on a channel rename is
    just ceremony.

    Blind is about the vote in progress, not the result. Every close
    publishes its numbers, this kind included -- see `finalize_bill`. What
    is secret forever is who cast which ballot, which is a different rule
    and a stronger one.
    """
    return bill.get("kind") in ("invite", "kick")


def removal_bar(guild, bill):
    """What carries a removal, and how many people that is out of.

    Not a share of anything, and deliberately so: it is a count of the
    people who would have to be convinced -- all of them bar a couple --
    which is also why a house counting against turnout does not move it. A
    removal settled among whoever turned up is three people showing
    somebody the door on a quiet Tuesday, and this bar exists to be
    unreachable that way.

    Eligibility is snapshotted at filing and intersected with the current
    keyholders, so a shrinking house cannot lower the bar mid-floor, and a
    cold member cache cannot either.
    """
    snapshot = set(bill.get("eligible_ids") or [])
    current = {
        m.id for m in getattr(guild, "members", ())
        if in_cooperative(m) and not m.bot and m.id != bill.get("target_id")
    }
    eligible = (snapshot & current) if snapshot else current
    held = numbers(guild)
    need = max(len(eligible) - held["removal_spare"], held["kick_min_yes"])
    return need, len(eligible)


def vote_state(guild, bill):
    """Everything the ballot needs to describe itself right now.

    One shape for every kind of vote. A choice ballot fills in `options`,
    `counts`, `leader` and `leaders` on top of the common turnout figures;
    a yes/no ballot leaves `options` empty. Anything describing a vote in
    words -- the ballot itself, the receipt, the nudge -- branches on that
    one key rather than keeping a second set of sums of its own.

    `need` is a count of yes votes and `counted` is what they are counted
    against: the roster out of the box, so not voting is a no, or the
    ballots cast where the house has said so. `clinch` is what ends the
    vote where it stands -- see `vote_settled`, which is the only place the
    difference between the two shows.
    """
    ballots = bill.get("ballots", {})
    roll = electorate(guild, bill)
    size = len(roll)
    tier = vote_tier(bill)
    figures = numbers(guild)
    share = roster.share_for(tier, figures)
    yes = sum(1 for v in ballots.values() if v == "yes")
    no = sum(1 for v in ballots.values() if v == "no")
    abstain = sum(1 for v in ballots.values() if v == "abstain")
    against = roster.counted(size, len(ballots), abstain, figures)
    st = {
        "size": size,
        "counted": against,
        "tier": tier,
        "audience": COOPERATIVE_ONLY,
        "need": roster.required(against, tier, share),
        "yes": yes,
        "no": no,
        "abstain": abstain,
        "voted": len(ballots),
        "waiting": max(size - len(ballots), 0),
        "options": bill.get("options") or None,
        "counts": {},
        "leader": 0,
        "leaders": [],
        # The bar measured against the widest this vote's denominator could
        # ever get. Counted against the roster that is the bar itself and
        # nothing changes; counted against turnout it is what the whole
        # roll would have asked for, because every ballot still to come
        # widens the denominator under a threshold already met.
        "clinch": roster.required(
            roster.most_counted(size, abstain, figures), tier, share),
        "round": bill.get("round", 1),
    }
    if bill.get("kind") == "kick":
        # A removal is carried on its own bar rather than on its tier's
        # share, and the ballot has to quote the number the close will use.
        # It read the fundamental share here and the bar at the close, so a
        # removal could say it needed six and pass on five.
        st["need"], st["counted"] = removal_bar(guild, bill)
        st["clinch"] = st["need"]
    if st["options"]:
        counts = {o: 0 for o in st["options"]}
        for v in ballots.values():
            if v in counts:
                counts[v] += 1
        leader = max(counts.values(), default=0)
        st["counts"] = counts
        st["leader"] = leader
        st["leaders"] = [o for o, n in counts.items() if n == leader and n > 0]
        # Passage is a majority of votes cast, counted at close -- but an
        # option past half the whole roster has that majority already,
        # whoever else turns up, so this is the count that ends the vote
        # where it stands. Neither counting switch reaches this: a choice
        # ballot is already decided among the votes cast, and there is no
        # abstain button to step out of them.
        st["clinch"] = size // 2 + 1
    return st


def bar(done, total, width=8):
    total = max(total, 1)
    filled = max(0, min(width, round(width * done / total)))
    return "█" * filled + "░" * (width - filled)


def choice_line(st):
    """The live standing of a choice ballot, on one line: turnout, every
    option with its count, and the number that would settle it.

    It used to be a bar per option, which is a vote that costs twelve lines
    to say what nine words say. The options are on the buttons underneath
    in the same order, so the line is read against them rather than
    repeating them at size.

    A choice ballot is a vote about a thing, so it shows everything, for
    the same reason `is_blind` gives -- hiding the standing on a question
    about a channel name is ceremony, and a running count is what tells
    people whether their option is worth arguing for while there is still
    time to argue.
    """
    tallies = " · ".join(
        f"**{o}** {n}" if n and n == st["leader"] else f"{o} {n}"
        for o, n in st["counts"].items()
    )
    return (f"`{bar(st['voted'], st['size'])}` {st['voted']} of {st['size']} "
            f"voted · {tallies} · {st['clinch']} carries it")


def ballot_line(guild, bill):
    """Where a vote stands, in one line and no more.

    Rewritten on every ballot cast, so progress toward the threshold is
    visible the whole way rather than arriving as a surprise at close. One
    line because the floor is a queue of these: what a voter needs is the
    bar, the count and the threshold, and everything that was around them
    -- that a vote about a person keeps its count quiet, that a ballot can
    be changed, that nobody ever sees how you voted -- is true of every
    vote there has ever been. It is said once, in the receipt for the
    ballot you just cast, where it is about you.
    """
    st = vote_state(guild, bill)
    if st["options"]:
        return choice_line(st)
    if is_blind(bill):
        return (f"`{bar(st['voted'], st['size'])}` {st['voted']} of "
                f"{st['size']} voted · needs {st['need']} yes")
    return (
        f"`{bar(st['yes'], st['need'])}` {st['yes']} of {st['need']} yes · "
        f"❌ {st['no']} · ⬜ {st['waiting']}"
        + (f" · 🤍 {st['abstain']}" if st["abstain"] else "")
    )


def closed_body(guild, bill):
    """What the ballot says once it is over.

    The whole of the close, on the message the vote was already on. Not a
    card under it: the floor is for votes that are open, and this is what
    is left of one that is not.
    """
    shown = bill.get("tally_line", "")
    if bill.get("status") == "vetoed":
        verdict = "Carried, and vetoed inside its window."
    elif bill.get("decided"):
        verdict = f"Decided: {bill['decided']}."
    else:
        verdict = "Passed." if bill.get("status") == "passed" else "Failed."
    line = f"**Ballot closed.** {verdict} {shown}"
    if bill.get("closed_early_by"):
        line += f" Closed early by {bill['closed_early_by']}."
    record = record_for(guild, bill)
    if record and bill.get("act"):
        line += f"\n-# Decision {bill['act']}, in {record.mention}."
    elif record:
        line += f"\n-# In {record.mention}."
    return line


# What and Why may be written to 4000 characters each. The floor shows an
# opening of one and a line of the other; anything cut goes whole into the
# thread, where length costs nobody anything.
FLOOR_WHAT = 300
FLOOR_WHY = 160


def clip(text, limit):
    """One run of text cut to a length at a word boundary. Returns the
    piece and whether anything was lost. Line breaks go with it: a proposal
    written as six short lines should cost the floor one."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text, False
    cut = text.rfind(" ", 0, limit)
    return text[:cut if cut > limit // 2 else limit].rstrip(" ,.;:") + "…", True


def floor_text(bill):
    """As much of the proposal as the floor shows, and the whole of it for
    the thread when that is less than all of it.

    The floor is a list of things to vote on, not the place to read them
    at length: a hundred proposals at full height is a hundred proposals
    nobody scrolls past the third of, and the ones further down are voted
    on by whoever had the patience. So it shows an opening of the What at
    reading size and a line of the Why under it, and the whole of both is
    a click away in the thread the card already carries -- which is the
    room the argument is in anyway.
    """
    what = (bill.get("what") or "").strip()
    why = (bill.get("why") or "").strip()
    shown_what, cut_what = clip(what, FLOOR_WHAT)
    shown_why, cut_why = clip(why, FLOOR_WHY)
    lines = [line for line in (shown_what,
                               f"-# Why: {shown_why}" if shown_why else "")
             if line]
    whole = "\n\n".join(f"### {label}\n{body}"
                        for label, body in (("What", what), ("Why", why))
                        if body)
    return "\n".join(lines), (whole if cut_what or cut_why else "")


def floor_segments(guild, bill, text=True):
    """Everything a proposal shows on the floor, drawn from scratch.

    One message from filing to close: the proposal, the live ballot and,
    in the end, the result, all on the card the thread hangs off. It used
    to be four messages and a fifth for the result, which is four more
    than a vote needs.

    And one segment rather than four, because a card of separated blocks
    with a heading over each is a page, and the floor is a queue: title,
    the proposal as far as it fits, and one line saying where the vote
    stands and when it shuts. Everything on it is something you would act
    on -- what it is, what it says, how close it is, how long you have.

    The thread is not named here. It hangs off this card, so Discord draws
    it under the message already, and pointing at it in words is one more
    line saying what the room is already showing.

    `text` is off for the one case where the proposal is already on the
    floor in its own right: a ballot posted before any of this, which sits
    under the What and the Why as separate messages of their own. Those
    cannot be edited into a card, so they are repainted as content, and
    repeating the proposal there would print it twice in the same screen.
    """
    open_now = bill.get("status") == "on_floor"
    try:
        ends = datetime.fromisoformat(bill["ends_at"])
        clock = f" · closes <t:{int(ends.timestamp())}:R>"
    except (KeyError, ValueError):
        clock = ""

    round_note = " (runoff)" if bill.get("round", 1) > 1 else ""
    # Marked because it is a claim on other people's inbox, and one nobody
    # should discover only by being direct-messaged about it -- or by
    # noticing that they were not. It leads the line so a floor of fifty
    # can be skimmed for the ones that will chase you.
    flag = "⚡ " if is_priority(bill) else ""
    lines = [f"**{flag}No. {bill['no']}: {bill['title']}**{round_note}"]
    shown, _ = floor_text(bill) if text else ("", "")
    if shown:
        lines.append(shown)
    if open_now and bill.get("runoff_note"):
        lines.append(f"-# {bill['runoff_note']}")
    if open_now:
        lines.append(f"-# {ballot_line(guild, bill)} · "
                     f"{bill.get('author', 'someone')}{clock}")
    else:
        lines.append(closed_body(guild, bill))
    return ["\n".join(lines)]


def ballot_content(guild, bill, text=True):
    """The card as one piece of text. What the floor actually shows."""
    return "\n\n".join(floor_segments(guild, bill, text))


async def find_card(channel, message_id):
    """The card, or what to do about not having it.

    Three answers, and they are not the same answer. The message, where it
    is there. `None` where Discord says it is gone, which is recoverable:
    put another one up. `False` where the lookup itself failed, which is
    not: something transient is wrong and reposting would leave two cards
    for one proposal, so that one waits and is tried again.
    """
    if channel is None or not message_id:
        return None
    try:
        return await channel.fetch_message(message_id)
    except discord.NotFound:
        return None
    except discord.HTTPException as e:
        log.warning(f"could not read message {message_id}: {e!r}")
        return False


async def paint_floor(guild, bill):
    """Redraw the proposal's one message on the floor. Best-effort: a vote
    that cannot redraw is still a valid vote, so a failure here never
    blocks one."""
    floor = floor_for(guild, bill)
    if guild is None or floor is None:
        return
    rows = ballot_rows(bill) if bill.get("status") == "on_floor" else ()
    view = Card(floor_segments(guild, bill), rows)
    msg = await find_card(floor, bill.get("ballot_message_id"))
    if msg is False:
        return
    if msg is None:
        # One message per proposal holds only while the message is there.
        # Somebody who deletes a live ballot must not thereby delete the
        # vote, so it goes back up and the proposal is told where.
        try:
            # Put back silently. The bell is rung when a proposal opens,
            # not when a card comes back from being deleted: whoever asked
            # to be told about this vote was told about it days ago.
            fresh = await floor.send(view=view, allowed_mentions=ring())
        except discord.HTTPException as e:
            return log.warning(f"could not put the floor card for bill "
                               f"{bill['no']} back up: {e!r}")
        bill["message_id"] = fresh.id
        bill["ballot_message_id"] = fresh.id
        await update_bill(guild, bill)
        return log.info(f"floor card for bill {bill['no']} was gone; reposted")
    try:
        # A card is edited on every vote, at close, and again at every
        # boot. None of those is a new proposal, so none of them may ping
        # anybody: an edit that is allowed a mention is a bell that rings
        # once a minute on a busy vote.
        await msg.edit(view=view, allowed_mentions=ring())
    except discord.HTTPException:
        # A ballot posted before the proposal became one card cannot be
        # edited into one: Discord will not put the layout on a message
        # that went up without it. Those keep their old shape and their
        # old buttons, which still answer, rather than freezing mid-vote.
        # Without the proposal's own words, which are already up there in
        # the messages that ballot was posted under.
        try:
            await msg.edit(content=ballot_content(guild, bill, text=False),
                           allowed_mentions=ring())
        except discord.HTTPException as e:
            log.warning(f"could not repaint the floor card for bill "
                        f"{bill['no']}: {e!r}")


async def refresh_ballot(guild, bill):
    if guild is None or bill.get("status") != "on_floor":
        return
    await paint_floor(guild, bill)


def unfinished(bill):
    """Whether a proposal still has something that has to be right.

    Three cases, and between them they are every card whose contents can
    still be wrong. A vote on the floor, whose counts move. A window still
    open, whose buttons have to work and whose clock has to run out. And a
    vote that closed without reaching the record, which is the one that
    matters most: the ruling is in the file and nowhere anybody can read
    it. Everything else is finished, said, and not worth an API call a
    deploy.
    """
    return (bill.get("status") == "on_floor"
            or veto_open(bill)
            or (bill.get("status") in ("passed", "failed", "vetoed")
                and not bill.get("record_message_id")))


async def repaint_cards(guild):
    """Redraw everything still unfinished. Runs at boot, alongside the
    furniture restamp and for the same reason: a card is edited for the
    whole of a proposal's life, so an edit that did not land -- a deploy
    mid-vote, a rate limit, a room that was briefly unreachable -- would
    otherwise stay wrong for good. This is where that comes right, and
    where a card somebody deleted goes back up.
    """
    for bill in load_json(bills_path(guild), []):
        if not unfinished(bill):
            continue
        try:
            if bill.get("status") == "on_floor":
                await paint_floor(guild, bill)
            else:
                await paint_record(guild, bill)
        except Exception as e:
            # One proposal that will not draw must not stop the next.
            log.error(f"could not settle the cards on bill "
                      f"{bill.get('no')}: {e!r}")


def vote_settled(st):
    """Whether a vote's result can still change.

    Passing is settled the instant the yes votes clear `clinch`, which is
    the threshold measured against the widest denominator this vote could
    still end up with. Counting against the roster that is the threshold
    itself, so the fifth yes of eight ends it where it stands. Counting
    against turnout it is what the whole roll would have asked for, because
    every ballot still to come widens the denominator underneath a bar that
    has already been met -- so a turnout vote is called early only where
    the yes votes alone would have carried the whole house, and otherwise
    waits, which is the price of that rule and not a bug in it.

    Failing waits for everyone either way, because a no can still become a
    yes while the vote is open -- an unreachable threshold is only genuinely
    unreachable once nobody is left to change their mind.

    A choice ballot is settled once an option is past half the roster --
    that is a majority of whoever ends up voting, however many that is --
    and otherwise waits for the room, since the leader can still change
    hands while the vote is open.

    An empty roster is never settled: that means we cannot see who is here,
    not that nobody is.
    """
    if st["size"] <= 0:
        return False
    if st["voted"] >= st["size"]:
        return True
    if st["options"]:
        # An option past half the room has a majority of however many end
        # up voting, whoever else turns up.
        return st["leader"] >= st["clinch"]
    return st["yes"] >= st["clinch"]


async def maybe_autoclose(guild, bill):
    """End a vote the moment vote_settled says it is over."""
    if bill.get("status") != "on_floor":
        return False
    if not vote_settled(vote_state(guild, bill)):
        return False
    await close_bill(guild, bill)
    if bill.get("status") == "on_floor":
        # A choice ballot with no majority reopens as a runoff rather than
        # closing. The house has spoken early, but the vote is not over,
        # so it is not an early close either.
        return False
    bill["closed_early"] = "settled"
    await update_bill(guild, bill)
    await post_closing_report(guild, bill)
    return True


def standing_line(guild, bill, carried="It already has what it needs."):
    """Where a vote stands, in one sentence and in the terms its own
    ballot uses. The receipt and the nudge both read from here, so the
    two can never describe the same vote differently. Only the wording
    for a vote that is already home differs between them: to the person
    who just cast it, it was their vote that did it."""
    st = vote_state(guild, bill)
    if st["options"]:
        if not st["leaders"]:
            return f"Nothing has a vote yet; {st['size']} can cast one."
        if len(st["leaders"]) > 1:
            return (f"{st['voted']} of {st['size']} have voted, and "
                    f"{len(st['leaders'])} options are level on {st['leader']}.")
        return (f"{st['voted']} of {st['size']} have voted, and "
                f"**{st['leaders'][0]}** leads with {st['leader']}.")
    if is_blind(bill):
        return f"{st['voted']} of {st['size']} have voted."
    if numbers(guild)[roster.TURNOUT] and not vote_settled(st):
        # Counted against turnout there is no fixed distance left to run: a
        # yes lifts the denominator as well as the count, so "one more
        # carries it" is a promise the arithmetic will not keep -- at three
        # cast it takes two, and the third yes makes it three of four.
        return (f"{st['yes']} of the {st['counted']} cast so far are yes and "
                f"it needs {st['need']} of them, but the bar moves with "
                f"every ballot that arrives.")
    left = max(st["need"] - st["yes"], 0)
    if left == 0:
        return carried
    return f"{left} more yes {'vote' if left == 1 else 'votes'} carries it."


async def cast_ballot(interaction, choice=None, index=None):
    """Record one ballot, or retract it when nothing is chosen.

    Shared by every ballot shape so they cannot drift apart on who may
    vote. A yes/no ballot names its `choice`; a choice ballot passes the
    `index` of the button pressed, which only means anything once the
    proposal has been found, since the options live on the proposal.
    """
    bill = bill_by(interaction.guild, "ballot_message_id", interaction.message.id)
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
    if index is not None:
        options = bill.get("options") or []
        if index >= len(options):
            return await interaction.response.send_message(
                "That option is not on this ballot.", ephemeral=True
            )
        choice = options[index]
    ballots = bill.setdefault("ballots", {})
    uid = str(interaction.user.id)
    if choice is None:
        if uid in ballots:
            del ballots[uid]
            await update_bill(interaction.guild, bill)
            await interaction.response.send_message(
                "Ballot retracted.", ephemeral=True
            )
            return await refresh_ballot(interaction.guild, bill)
        return await interaction.response.send_message(
            "You have no ballot to retract.", ephemeral=True
        )
    ballots[uid] = choice
    await update_bill(interaction.guild, bill)
    # What an abstention does is the one thing on this receipt a house can
    # change, and the person casting one is owed the version in force here
    # rather than the one the clerk shipped with.
    if choice != "abstain":
        note = "You can change it until the vote closes."
    elif numbers(interaction.guild)[roster.ABSTAIN_OUT]:
        note = ("Counted as present and undecided, and out of the count "
                "altogether: it lowers what carries this rather than "
                "standing in the way of it.")
    else:
        note = "Counted as present and undecided; it goes to neither side."
    await interaction.response.send_message(
        f"Your ballot: **{choice}**. {note} "
        f"{standing_line(interaction.guild, bill, carried='That carries it.')} "
        f"Nobody, including the author, will ever see how you voted.",
        ephemeral=True,
    )
    await refresh_ballot(interaction.guild, bill)
    await maybe_autoclose(interaction.guild, bill)


class BallotRow(discord.ui.ActionRow):
    """A yes/no ballot, as a row rather than a view of its own, so it can
    sit on the proposal's card instead of on a message under it."""

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


class MemberBallotRow(discord.ui.ActionRow):
    """The ballot for admitting someone. Three choices instead of two,
    because in a house this small "I do not know them well enough to say"
    is an honest answer, and the two-button ballot made it look like
    absence. It goes to neither side: passage is still yes against the bar,
    which is what the standing orders say. Whether it also comes out of the
    count is the house's -- `abstain_steps_out` -- and this is the one
    ballot in the building that offers the button, so it is the one that
    setting is really about."""

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


class MultiBallotRows(list):
    """Choice ballot: one button per option, across as many rows as it
    takes. A bare instance with dummy labels handles routing after
    restarts; the real labels live on the message itself.

    The buttons are the only thing here that differs from a yes/no ballot.
    Everything a press then does -- who may vote, recording it, repainting
    the message, ending the vote once it is decided -- is `cast_ballot`,
    the same as every other ballot in the server.
    """

    def __init__(self, options=None):
        super().__init__()
        labels = options if options is not None else [
            f"Option {i + 1}" for i in range(MULTI_MAX)
        ]
        row = discord.ui.ActionRow()
        for i, label in enumerate(labels[:MULTI_MAX]):
            if len(row.children) == 5:
                self.append(row)
                row = discord.ui.ActionRow()
            button = discord.ui.Button(
                label=str(label)[:80],
                style=discord.ButtonStyle.primary,
                custom_id=f"clerk:opt_{i}",
            )
            button.callback = self._make_callback(i)
            row.add_item(button)
        retract = discord.ui.Button(
            label="Retract",
            style=discord.ButtonStyle.secondary,
            custom_id="clerk:opt_retract",
        )
        retract.callback = self._retract
        # On the end of the options where they leave room for it. A row of
        # its own for one grey button is a line of card per ballot, and the
        # floor pays that on every choice vote open at once.
        if len(row.children) == 5:
            self.append(row)
            row = discord.ui.ActionRow()
        row.add_item(retract)
        self.append(row)

    @property
    def children(self):
        """Every button across the rows, for anything counting them."""
        return [button for row in self for button in row.children]

    def _make_callback(self, index):
        async def callback(interaction):
            await cast_ballot(interaction, index=index)
        return callback

    async def _retract(self, interaction):
        await cast_ballot(interaction)


def ballot_rows(bill):
    """The buttons a proposal's card carries, by what kind of vote it is."""
    if bill.get("options"):
        return MultiBallotRows(bill["options"])
    if bill.get("kind") == "invite":
        return [MemberBallotRow()]
    return [BallotRow()]


async def file_bill(guild, author, title, what, why, kind="ordinary",
                    options=None, target_id=None, floor_hours=None,
                    eligible_ids=None, priority=False, invitee=None):
    """Shared filing pipeline for all proposal kinds. Returns the filed
    proposal,
    or None if the room it belongs in is missing. Callers own their
    acknowledgement, since Eugene also files on request in conversation,
    where there is no interaction to reply to.

    """
    audience = COOPERATIVE_ONLY
    floor = floor_for(guild, {"audience": audience})
    if floor is None:
        return None
    number = await next_bill_number(guild)
    ends_at = now_utc() + timedelta(
        hours=floor_hours or numbers(guild)["floor_hours"]
    )

    record = {
        "no": number,
        "title": title,
        "kind": kind,
        "audience": audience,
        "priority": bool(priority),
        "target_id": target_id,
        # The name as it was typed, kept apart from the title so the DM
        # after an invitation carries can say who it is for without
        # reading a sentence back for it.
        "invitee": invitee,
        "eligible_ids": eligible_ids,
        "author_id": author.id,
        "author": author.display_name,
        "what": what,
        "why": why,
        "submitted_at": now_utc().isoformat(),
        "ends_at": ends_at.isoformat(),
        "status": "on_floor",
        "options": options or None,
        "round": 1,
        "ballots": {},
        "notes": {},
    }
    # One message, and it is the proposal, the ballot and in the end the
    # result. The buttons are the only thing that varies by kind; the card
    # above them is painted from the proposal itself, so a ballot shows
    # what it needs from its first second rather than once somebody votes.
    bell = bell_for(guild)
    segments = floor_segments(guild, record)
    if bell is not None:
        # The ping rides on the card. A mention needs to be in the text to
        # be a mention at all, so it goes on the end of the line that
        # already says where the ballot stands rather than earning a line
        # of its own -- and it is written here, once, at the open. Every
        # repaint after this is drawn from the proposal alone, so the
        # mention is gone by the first vote and no edit can ring it again.
        segments[-1] += f" · {bell.mention}"
    card = await floor.send(
        view=Card(segments, ballot_rows(record)),
        allowed_mentions=ring(bell),
    )
    record["message_id"] = card.id
    record["ballot_message_id"] = card.id

    # One thread per proposal, and it hangs off the proposal itself. There
    # was briefly a second one beside it for the argument, which meant two
    # rooms in the sidebar for one vote and a guess to make before typing
    # about which of them a thought belonged in. A week, because a vote runs
    # for days and a thread that files itself away mid-argument reads as the
    # argument being over.
    notes_thread = await card.create_thread(
        name=(bill_name(number, title) + ": notes")[:100],
        auto_archive_duration=10080,
    )
    record["notes_thread_id"] = notes_thread.id
    # Whatever the card had to cut goes here whole, ahead of the prompt and
    # first in the thread, so the floor can be a list without the proposal
    # being shortened out of anywhere it can be read.
    _, overflow = floor_text(record)
    for piece in chunk_text(overflow) if overflow else ():
        await notes_thread.send(piece)
    notes_msg = await notes_thread.send(NOTES_PROMPT, view=NotesView())
    record["notes_message_id"] = notes_msg.id

    path = bills_path(guild)
    bills = load_json(path, [])
    bills.append(record)
    save_json(path, bills)
    log.info(f"proposal filed: no. {number} ({title!r}, {kind}) by {author.display_name}")
    return record


async def file_from_modal(interaction, **kwargs):
    """Filing from a button, with the ephemeral receipt that expects."""
    bill = await file_bill(interaction.guild, interaction.user, **kwargs)
    if bill is None:
        return await interaction.followup.send(
            "There is no room bound for the `votes` job, so there is "
            "nowhere to put this. An admin can point me at one with "
            "`/setup`.",
            ephemeral=True,
        )
    floor = floor_for(interaction.guild, bill)
    chamber = chamber_of(interaction.guild, bill)
    where = f", debate in {chamber.mention}" if chamber else ""
    await interaction.followup.send(
        f"Filed. Proposal No. {bill['no']} is open: {floor.mention}{where}.",
        ephemeral=True,
    )
    return bill


# ---------- submitting bills ----------

# The two proposals Eugene writes the What for himself. A person writing
# one says what they mean; these are generated, and a generated sentence
# is the same sentence under every name, so it says the one thing that
# differs and stops. Both used to spell out the mechanism as well -- that
# an invitation is a single-use link, valid seven days, sent privately,
# and a place in the room rather than in the cooperative; that a removal
# needs all but two, that the subject cannot vote and keeps the window to
# plead, that the tally is never published -- which is four sentences
# nobody is voting on, since they are true of every invitation and every
# removal there will ever be. They are in the standing orders, they are on
# the ballot's own line where they are live figures rather than the count
# at filing, and they are in the receipt at the moment they take effect.
# Here they were just the height that pushed the next proposal off screen.

def invite_what(guild, name, discord_id=""):
    """The What on an invitation: who, and where."""
    tail = f" (Discord ID {discord_id})" if discord_id else ""
    return f"{name}{tail} will be invited to {guild.name}."


def removal_what(guild, name):
    """The What on a removal: who, and where from."""
    return f"{name} will be removed from {guild.name}."


def removal_weight(guild):
    """What a removal costs, said before anybody names a person.

    Both doors onto a removal say it, so it is written once and in this
    house's own figures. One of them had the window and the bar typed out
    as words, which stayed the shipped 72 hours and all-but-two however the
    house had since voted -- a sentence quoting a rule nothing enforced,
    to the person about to use it.
    """
    held = numbers(guild)
    spare = held["removal_spare"]
    return (
        "Removal is the cooperative's heaviest instrument: a "
        f"{held['removal_hours']:g}-hour window, and it passes only if all "
        f"eligible voters but {spare} say yes."
    )


class BillModal(discord.ui.Modal, title="Make a proposal"):
    def __init__(self, priority=False, prefill=None):
        super().__init__()
        self.priority = bool(priority)
        # A draft he wrote arrives filled in and every word of it editable,
        # which is the difference between an offer and a fait accompli. The
        # person who presses send is the author, so they had better be able
        # to disagree with the wording first.
        if prefill:
            self.bill_title.default = (prefill.get("title") or "")[:100]
            self.what.default = (prefill.get("what") or "")[:4000]
            self.why.default = (prefill.get("why") or "")[:4000]

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
        try:
            options = parse_options(self.choices)
        except ValueError as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        bill = await file_from_modal(
            interaction,
            title=str(self.bill_title),
            what=str(self.what),
            why=str(self.why),
            options=options,
            priority=self.priority,
        )

def parse_options(raw):
    """One option per line, duplicates dropped, order kept. Returns the
    list, or raises ValueError with the sentence to say back."""
    options = []
    for line in str(raw).splitlines():
        line = line.strip()
        if line and line not in options:
            options.append(line)
    if options and not 2 <= len(options) <= MULTI_MAX:
        raise ValueError(
            f"A choice ballot needs 2 to {MULTI_MAX} distinct options "
            f"(one per line), or none at all for a yes/no ballot."
        )
    return options or None


class InviteModal(discord.ui.Modal, title="Propose an invitation"):
    """Somebody outside the server, proposed into it. What passes is a link
    and a place in the room -- never a vote, and never the cooperative,
    which is handed over under `/setup` and has no ballot at all."""


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
        placeholder="Why should they be in the server?",
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        name = str(self.invitee).strip()
        given = str(self.discord_id).strip()
        await file_from_modal(
            interaction,
            title=f"Invitation of {name}"[:100],
            what=invite_what(interaction.guild, name, given),
            why=str(self.why),
            kind="invite",
            invitee=name,
            # Kept because a vetoed invitation has to find whoever came
            # through it, and a name in a sentence is not something to
            # remove somebody on.
            target_id=int(given) if given.isdigit() else None,
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
        eligible_ids = [
            m.id for m in self.target.guild.members
            if in_cooperative(m) and not m.bot and m.id != self.target.id
        ]
        await file_from_modal(
            interaction,
            title=f"Removal of {self.target.display_name}"[:100],
            what=removal_what(self.target.guild, self.target.display_name),
            why=str(self.why),
            kind="kick",
            target_id=self.target.id,
            floor_hours=numbers(self.target.guild)["removal_hours"],
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
        for b in load_json(bills_path(guild), []):
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
            removal_weight(interaction.guild),
            view=KickTargetView(),
            ephemeral=True,
        )


# ---------- closing the floor ----------

def number_act(guild, bill, decided=None):
    """Give a decision its number and put it in the book.

    The book is acts.json and it is the record that matters: what gets
    posted is a copy of it, and a long decision is trimmed in the copy
    while the book keeps every word.
    """
    acts = load_json(acts_path(guild), [])
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
    save_json(acts_path(guild), acts)
    bill["act"] = act_no
    return act_no


# A card holds 4000 characters and a proposal may be written to 4000 of
# its own, so the longest ones do not fit under a heading and a foot.
RECORD_LIMIT = 3200


def record_segments(guild, bill):
    """Everything a proposal leaves in the record, drawn from scratch.

    One message per proposal, edited as it changes: numbered when it
    carries, marked when the window shuts, struck if it is taken back. A
    decision that arrives in three messages and grows two more over the
    next day is five things to scroll for one thing that happened.
    """
    passed = bill.get("status") == "passed"
    struck = bill.get("status") == "vetoed"
    act = bill.get("act")

    if struck:
        head = (f"## Decision {act}: struck" if act
                else f"## Proposal No. {bill['no']}: overturned")
        head += (f"\n{bill['title']}: carried, then vetoed inside its "
                 f"window. The number is kept struck rather than reused.")
    elif passed and act:
        head = f"## Decision {act}: {bill['title']}"
    else:
        head = (f"## Proposal No. {bill['no']}: {bill['title']}\n"
                f"**{'Passed' if passed else 'Failed'}**")
    segments = [head]

    body = (bill.get("what") or "").strip()
    if body and not struck:
        if len(body) > RECORD_LIMIT:
            body = (body[:RECORD_LIMIT].rstrip()
                    + "…\n-# Trimmed here. Ask Eugene for it in full.")
        segments.append(body)
    if bill.get("decided") and not struck:
        segments.append(f"### Decided\n{bill['decided']}")

    foot = (f"-# From Proposal No. {bill['no']} by "
            f"{bill.get('author', 'someone')}, "
            f"{'passed' if passed or struck else 'failed'} with "
            f"{bill.get('tally_line', 'no tally')}.")
    done = bill.get("carried_out") or {}
    if done.get("by"):
        foot += f" Carried out by {done['by']}."
    window = veto_line(bill)
    if window:
        foot += f" {window}"
    segments.append(foot)

    # Dropped the moment somebody says they have done it, because a
    # decision that keeps asking after it has been carried out is the
    # reason nobody reads the ones that are still asking.
    if bill.get("outstanding") and not done:
        segments.append("### Still wanted\n"
                        + "\n".join(f"- {line}" for line in bill["outstanding"]))
    return segments


async def paint_record(guild, bill):
    """Put the proposal in the record, or redraw the copy already there.

    Best-effort on the redraw, like the ballot: a card that will not repaint
    is not a reason to lose what the repaint was about.
    """
    where = outcome_for(guild, bill)
    if where is None:
        return
    view = Card(record_segments(guild, bill),
                [VetoRow()] if veto_open(bill) else ())
    mid = bill.get("record_message_id")
    channel = guild.get_channel(bill.get("record_channel_id") or 0) or where
    msg = await find_card(channel, mid) if mid else None
    if msg is False:
        return
    if msg is not None:
        try:
            return await msg.edit(view=view)
        except discord.HTTPException as e:
            return log.warning(f"could not repaint the record on bill "
                               f"{bill['no']}: {e!r}")
    # Either it has never been in the record, or the card that was there
    # has been deleted. Both want the same thing, and a decision that is
    # not in the record is the worse of the two to leave alone.
    try:
        msg = await where.send(view=view)
    except discord.HTTPException as e:
        log.warning(f"could not post bill {bill['no']} to the record: {e!r}")
        return
    if mid:
        log.info(f"record card for bill {bill['no']} was gone; reposted")
    bill["record_channel_id"] = where.id
    bill["record_message_id"] = msg.id
    await update_bill(guild, bill)


async def seal_chamber(guild, bill):
    """Shut a debate room that is not the proposal's own thread.

    A proposal filed now argues in the thread its notes are in, and that
    one is sealed with the notes a few lines above this is called: there is
    nothing left here to do, and this does nothing. What is left is the two
    shapes that came before it, both of which are still open on somebody's
    floor. A proposal filed with a separate debate thread has that thread
    locked and archived where it stands. One older still holds a category,
    a text channel and a voice channel -- text locked and filed in the
    archive, voice deleted because voice is never recorded, the empty
    category swept up after them.
    """
    thread_id = bill.get("chamber_thread_id")
    if thread_id:
        try:
            thread = guild.get_thread(thread_id) or await guild.fetch_channel(thread_id)
            await thread.send("*Vote closed. The debate is sealed with it.*")
            await thread.edit(archived=True, locked=True)
        except discord.HTTPException as e:
            log.warning(f"sealing debate thread failed for bill {bill['no']}: {e!r}")
        return

    text = guild.get_channel(bill.get("chamber_text_id"))
    archive = archive_category(guild)
    if text and archive:
        await text.edit(
            name=bill_name(bill["no"], bill["title"], "archived_"),
            category=archive,
            sync_permissions=False,
            overwrites=await hidden_overwrites(guild),
        )
    voice = guild.get_channel(bill.get("chamber_voice_id"))
    if voice:
        await voice.delete(reason="Vote closed; voice is never recorded")
    category = guild.get_channel(bill.get("chamber_category_id"))
    if category and not category.channels:
        await category.delete(reason="Vote closed")


# ---------- the last word ----------
# A vote that has closed is not always over. A house may keep a window
# open after a proposal carries, in which anybody who could have voted on
# it can take it back, and enough of them do take it back.
#
# It exists for one case and generalises to the rest: the door. An
# invitation is the only decision here whose cost falls on everybody in
# the room and whose benefit is usually one person's, and a majority is a
# thin thing to hand somebody a key on. So the veto starts on for
# invitations and off for everything else -- a veto over every proposal is
# a permanent hold for whoever wants one most, and a house should have to
# ask for that on purpose rather than find it switched on.
#
# Both halves are the house's: `/setup` -> what we vote by, or a sentence
# said to Eugene. Nothing here is a constant.

# kind -> (the switch that allows it, the number that carries it).
VETO_RULES = {"invite": ("invite_veto", "invite_vetoes")}
VETO_DEFAULT_RULE = ("proposal_veto", "proposal_vetoes")


def veto_rule(guild, bill):
    """How many vetoes overturn this proposal, or None where none can.

    Read fresh on every press, like every other threshold here: a house
    that switches the veto off has switched it off for the windows already
    open, and one that raises the count has raised it for them too.
    """
    switch, count = VETO_RULES.get(bill.get("kind"), VETO_DEFAULT_RULE)
    held = numbers(guild)
    if not held[switch]:
        return None
    return max(int(held[count]), 1)


def veto_open(bill, at=None):
    """Whether a passed proposal can still be taken back.

    A proposal that never carried has nothing to veto: failing is already
    the answer the veto would give.
    """
    veto = bill.get("veto")
    if not veto or veto.get("closed") or bill.get("status") != "passed":
        return False
    try:
        return (at or now_utc()) < datetime.fromisoformat(veto["until"])
    except (KeyError, TypeError, ValueError):
        return False


def vetoes(n):
    return f"{n} veto" if n == 1 else f"{n} vetoes"


def veto_line(bill):
    """The window, as one line at the foot of the record.

    Who has vetoed is shown only where the house has not asked for the
    veto to be anonymous. That switch is read when the veto is cast, not
    when this is drawn: somebody who vetoed under a rule that named them
    is named, and somebody who vetoed under one that did not, never is.
    """
    veto = bill.get("veto")
    if not veto:
        return ""
    cast = veto.get("cast") or []
    needed = max(int(veto.get("needed", 1)), 1)

    if veto.get("closed"):
        if veto.get("overturned"):
            return ""
        held = veto.get("count", 0)
        return (f"🛑 The window closed with {vetoes(held)} of the {needed} "
                f"it wanted." if held else "🛑 The window closed unvetoed.")
    if not veto_open(bill):
        return ""

    try:
        shuts = int(datetime.fromisoformat(veto["until"]).timestamp())
        clock = f"can be taken back until <t:{shuts}:R>"
    except (KeyError, TypeError, ValueError):
        clock = "can still be taken back"
    named = [c["name"] for c in cast if c.get("name")]
    # `left` reaches zero when a house lowers the count under a window that
    # is already open. Rare, and "0 more" is nonsense in the one place
    # somebody would be reading closely.
    left = max(needed - len(cast), 0)
    if named:
        standing = f"vetoed by {', '.join(named)}"
    elif cast:
        standing = f"{vetoes(len(cast))} cast"
    else:
        standing = f"{vetoes(needed)} would overturn it"
    tail = f", {left} more to overturn" if cast and left else ""
    return f"🛑 {clock} · {standing}{tail}."


async def refresh_veto(guild, bill):
    await paint_record(guild, bill)


async def open_veto(guild, bill):
    """Hold a window open on a proposal that has just carried.

    It is a line and two buttons at the foot of the decision in the record,
    not a message of its own: the floor is for voting, and a vote that has
    closed should not still be taking up room on it.
    """
    needed = veto_rule(guild, bill)
    if needed is None:
        return
    hours = numbers(guild)["veto_hours"]
    bill["veto"] = {
        "until": (now_utc() + timedelta(hours=hours)).isoformat(),
        "needed": needed,
        "cast": [],
    }


def seal_veto(bill, overturned=False):
    """Shut the window and destroy what it held.

    The identities go the way the ballots go, and for the same reason: the
    count is the record, the people are not. What survives is how many,
    and -- where the house had not asked for anonymity, so the names were
    already public on the floor -- who.
    """
    veto = bill.get("veto")
    if not veto:
        return
    cast = veto.pop("cast", []) or []
    veto["count"] = len(cast)
    names = [c["name"] for c in cast if c.get("name")]
    if names:
        veto["by"] = names
    veto["closed"] = True
    veto["overturned"] = bool(overturned)


async def close_veto(guild, bill):
    """The window running out with the proposal still standing."""
    seal_veto(bill)
    await paint_record(guild, bill)
    await update_bill(guild, bill)
    log.info(f"veto window closed on bill {bill['no']}, unused")


async def annul_act(guild, bill):
    """A decision taken back is struck from the record, never removed from
    it. The number stays and says what happened to it: a record with a hole
    where Decision 12 used to be is a record nobody can trust."""
    acts = load_json(acts_path(guild), [])
    for act in acts:
        if act.get("act") == bill.get("act"):
            act["annulled"] = "vetoed"
            act["annulled_at"] = bill.get("vetoed_at")
    save_json(acts_path(guild), acts)


async def revoke_invite(guild, bill):
    """Take the door back.

    The link dies if nobody has spent it. If somebody has, they go back
    out -- which is the whole reason the window is short, and why what
    could not be undone is said out loud rather than left for somebody to
    discover.

    Returns (done, left): what happened, and what wants human hands.
    """
    done, left = [], []
    code = bill.get("invite_code")
    spent = None
    if code:
        try:
            live = await guild.invites()
            match = next((i for i in live if i.code == code), None)
            if match is not None:
                await match.delete(reason=f"Vetoed: Proposal No. {bill['no']}")
                done.append("The link is dead, and it was never used.")
                spent = False
            else:
                # A single-use invite leaves Discord the moment it is
                # spent, so a code that was issued and is not there is one
                # somebody walked through.
                spent = True
        except discord.HTTPException as e:
            log.warning(f"could not revoke the invite on bill {bill['no']}: {e!r}")
            left.append(
                "The invite link could not be revoked; somebody with the "
                "permission has to delete it by hand."
            )

    # Only somebody who can be shown to have come in on this link. Two
    # ways to know: he watched them arrive on it, or the proposal named an
    # id and the link is gone. A named id alone proves nothing -- the
    # person a proposal is about may be standing in the server already, or
    # have come in by another door entirely, and removing them for that
    # would be the veto reaching somebody it was never about.
    came_in = bill.get("joined_id") or (bill.get("target_id") if spent else None)
    arrival = guild.get_member(came_in) if came_in else None
    if arrival is not None:
        # Three people this window does not reach. Somebody on the roll,
        # because taking them off it is §7's fundamental vote and a veto on
        # the door is not a way around it. The owner, because Discord will
        # not have it. A bot, because a bot did not come in on an
        # invitation. Each is said rather than quietly skipped.
        why_not = (
            f"is in the cooperative now, and removing one of those is a "
            f"fundamental vote and not this" if in_cooperative(arrival)
            else "owns the server" if arrival.id == guild.owner_id
            else "is a bot" if arrival.bot else None
        )
        if why_not:
            left.append(
                f"{arrival.display_name} came in on this and {why_not}. "
                f"Eugene has left them where they are."
            )
        else:
            try:
                await arrival.send(
                    f"{guild.name} has taken back the invitation you came in "
                    f"on. It is nothing you did: the cooperative keeps a "
                    f"window to reverse one, and it used it."
                )
            except discord.HTTPException:
                pass
            try:
                await arrival.kick(
                    reason=f"Invitation vetoed: Proposal No. {bill['no']}"
                )
                done.append(f"{arrival.display_name} had already joined, and "
                            f"has been removed.")
            except discord.HTTPException as e:
                log.warning(f"could not remove {arrival.id} on bill {bill['no']}: {e!r}")
                left.append(
                    f"{arrival.display_name} came in on this and could not be "
                    f"removed. Somebody has to do it by hand."
                )
    elif came_in:
        done.append("The link was spent, and whoever used it has since left "
                    "of their own accord.")
    elif spent:
        done.append("The link was already spent.")
        left.append(
            "Somebody came through the link before it was taken back and "
            "Eugene cannot tell who. Whoever it was is still in the server."
        )

    proposer = guild.get_member(bill.get("author_id") or 0)
    if proposer is not None:
        try:
            await proposer.send(
                f"Proposal No. {bill['no']} has been vetoed inside its "
                f"window. The invitation is withdrawn and the link no longer "
                f"works; do not send it on."
            )
        except discord.HTTPException:
            pass
    return done, left


async def overturn_bill(guild, bill):
    """A proposal the house has taken back inside its window.

    Not the same thing as failing, and it is not recorded as one: it
    carried, and then it was reversed. Both halves are true and the record
    keeps both.
    """
    bill["status"] = "vetoed"
    bill["vetoed_at"] = now_utc().isoformat()
    seal_veto(bill, overturned=True)
    veto = bill.get("veto") or {}

    # What the reversal managed is not worth a paragraph -- the record says
    # it was struck. What it could not manage is, because that wants hands.
    left = []
    if bill.get("kind") == "invite":
        _, left = await revoke_invite(guild, bill)
    elif bill.get("kind") == "kick":
        left.append(
            "The removal had already been carried out. Eugene cannot undo "
            "one; somebody has to invite them back."
        )
    if bill.get("act"):
        await annul_act(guild, bill)

    names = veto.get("by")
    who = ("Vetoed by " + ", ".join(names) if names
           else f"Vetoed, by {vetoes(veto.get('count', 0))}")
    # The reversal is the same message the decision was, struck. Nothing
    # goes back to the floor: the vote there closed a day ago, and nothing
    # new is posted here either -- a decision that is taken back should not
    # cost the record two more cards to say so.
    if left:
        bill["outstanding"] = left
    await paint_record(guild, bill)
    # And the closed ballot, so a floor somebody scrolls back through does
    # not still show it as having carried.
    await paint_floor(guild, bill)

    await update_bill(guild, bill)
    await health_log(guild, f"🛑 Proposal No. {bill['no']} vetoed after passing.")
    log.info(f"bill overturned: no. {bill['no']} ({who})")
    await update_outstanding(guild, silent=True)


def bill_at(guild, message_id):
    """The proposal a pressed card belongs to.

    Two fields, because a window that was open when the last word moved
    onto the decision is still sitting on a message of its own, and its
    buttons have to keep working.
    """
    return (bill_by(guild, "record_message_id", message_id)
            or bill_by(guild, "veto_message_id", message_id))


def veto_refusal(guild, bill, user):
    """Why this press cannot be a veto, or None where it can."""
    if bill is None or not veto_open(bill):
        return "That window has closed."
    if not may_vote(bill, user):
        return refusal_for(bill)
    if veto_rule(guild, bill) is None:
        return "This house no longer keeps a veto over this kind of proposal."
    return None


async def veto_reply(interaction, text, replacing=False):
    """Answer a press. One that came by way of the confirmation replaces it,
    so the question does not sit there already answered."""
    if replacing:
        await interaction.response.edit_message(content=text, view=None)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def confirm_veto(interaction):
    """The press before the veto.

    At the house's default of one, the button is the whole thing: the link
    dies and whoever walked through it goes back out, with no window left
    to withdraw in. That is too much to hang on a misclick under a card
    somebody was only reading, so it asks first and nothing is recorded
    until the second press.
    """
    guild = interaction.guild
    bill = bill_at(guild, interaction.message.id)
    refusal = veto_refusal(guild, bill, interaction.user)
    if refusal:
        return await veto_reply(interaction, refusal)
    cast = bill["veto"].get("cast") or []
    if any(c.get("id") == interaction.user.id for c in cast):
        return await veto_reply(
            interaction,
            "Your veto is already on this. Withdraw it if you have changed "
            "your mind.",
        )

    left = max(veto_rule(guild, bill) - len(cast), 0)
    if left <= 1:
        undone = ("the link dies and whoever came through it goes back out"
                  if bill.get("kind") == "invite"
                  else "the decision is struck from the record")
        stake = f"Yours is the last one wanted, so {undone} at once."
    else:
        stake = (f"{left - 1} more would be wanted after yours, and you can "
                 f"withdraw it while the window is open.")
    await interaction.response.send_message(
        f"🛑 **Veto Proposal No. {bill['no']}?** {stake}",
        view=VetoConfirm(interaction.message.id),
        ephemeral=True,
    )


async def cast_veto(interaction, withdraw=False, host_id=None):
    """One veto, or the taking back of one.

    Withdrawing exists because the ballot has a retract and this is a
    heavier thing than a ballot. A veto somebody cast in the first hour
    and thought better of in the second should not be a proposal nobody
    can revive.
    """
    guild = interaction.guild
    # A confirmed veto is pressed on the ephemeral question, not on the
    # record, so the card it is about has to be carried to it.
    replacing = host_id is not None
    bill = bill_at(guild, host_id or interaction.message.id)
    refusal = veto_refusal(guild, bill, interaction.user)
    if refusal:
        return await veto_reply(interaction, refusal, replacing)

    needed = veto_rule(guild, bill)
    veto = bill["veto"]
    veto["needed"] = needed
    cast = veto.setdefault("cast", [])
    mine = next((c for c in cast if c.get("id") == interaction.user.id), None)
    if withdraw:
        if mine is None:
            return await veto_reply(
                interaction, "You have no veto on this to withdraw.", replacing
            )
        cast.remove(mine)
        word = "Veto withdrawn."
    elif mine is not None:
        return await veto_reply(
            interaction,
            "Your veto is already on this. Withdraw it if you have changed "
            "your mind.",
            replacing,
        )
    else:
        entry = {"id": interaction.user.id}
        if not numbers(guild)["veto_anonymous"]:
            entry["name"] = interaction.user.display_name
        cast.append(entry)
        word = ("Vetoed. It is named on the record." if "name" in entry
                else "Vetoed. Nobody is told it was you.")

    await update_bill(guild, bill)
    left = max(needed - len(cast), 0)
    tail = ("That overturns it." if left == 0 and not withdraw
            else f"{vetoes(left)} would overturn it.")
    await veto_reply(interaction, f"{word} {tail}", replacing)

    if not withdraw and len(cast) >= needed:
        return await overturn_bill(guild, bill)
    await refresh_veto(guild, bill)


class VetoRow(discord.ui.ActionRow):
    """The window, as two buttons, kept as a row so it can sit inside the
    decision's own card instead of on a message of its own.

    Neither is red. A red button on a card everybody reads is a button
    somebody presses, and the thing behind this one is the heaviest reversal
    the house has.
    """

    @discord.ui.button(
        label="Veto", emoji="🛑",
        style=discord.ButtonStyle.secondary, custom_id="clerk:veto",
    )
    async def veto(self, interaction, button):
        await confirm_veto(interaction)

    @discord.ui.button(
        label="Withdraw", emoji="↩️",
        style=discord.ButtonStyle.secondary, custom_id="clerk:veto_withdraw",
    )
    async def withdraw(self, interaction, button):
        await cast_veto(interaction, withdraw=True)


class VetoConfirm(discord.ui.View):
    """The question between the button and the veto. Ephemeral and
    short-lived on purpose: it is a question, not a record. It carries the
    id of the card it was opened from, because the press that answers it
    lands on a message of its own."""

    def __init__(self, host_id):
        super().__init__(timeout=180)
        self.host_id = host_id

    @discord.ui.button(label="Yes, veto it", emoji="🛑",
                       style=discord.ButtonStyle.secondary)
    async def yes(self, interaction, button):
        await cast_veto(interaction, host_id=self.host_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction, button):
        await interaction.response.edit_message(
            content="Nothing was cast.", view=None
        )


async def finalize_bill(guild, bill, passed, tally_line, decided=None):
    """Common closing: the ballot shut where it stands, the ruling put in
    the record, the notes and the debate sealed.

    Nothing new is posted to the floor. The room is for votes that are open
    and the ballot itself is already sitting there, so the result is an
    edit to it rather than a card under it, and everything else about the
    close belongs to the record.
    """
    bill["status"] = "passed" if passed else "failed"
    bill["closed_at"] = now_utc().isoformat()
    # the tally is all the record needs from here; individual ballots are
    # destroyed at close so a closed vote cannot be reconstructed by anyone
    bill.pop("ballots", None)
    # Every close publishes its numbers, people-bills included. They used to
    # print "a sealed tally" while acts.json kept the real figures anyway,
    # so the seal was on the card and not on the data: a decision the house
    # could read out of a file but not off its own record. One rule now --
    # the split is public, and who cast which ballot never is.
    bill["tally_line"] = tally_line
    if decided:
        bill["decided"] = decided
    floor = floor_for(guild, bill)

    if passed:
        number_act(guild, bill, decided)

    if floor:
        thread_id = bill.get("notes_thread_id")
        if thread_id:
            # The thread is being locked, so the last thing in it says why
            # rather than leaving somebody to work out that it went quiet.
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

    await seal_chamber(guild, bill)

    # The window opens before either card is drawn, so each goes up once
    # already saying what it will say rather than being edited a moment
    # later. The number has to exist by then too, for the floor to point at.
    if passed:
        await open_veto(guild, bill)
    await paint_record(guild, bill)
    if bill.get("veto") and not bill.get("record_message_id"):
        # The card the buttons were going on never went up. A window
        # nobody can reach is not a window, and it must not be one the
        # closing report goes on to promise.
        bill.pop("veto", None)
        log.warning(f"no record card for bill {bill['no']}, so no window")
    await paint_floor(guild, bill)

    await update_bill(guild, bill)
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
    overturned = bill["status"] == "vetoed"
    kind = bill.get("kind", "ordinary")

    done = [
        "The vote is closed and the ballots are destroyed; only the tally survives.",
        "The notes and the debate are sealed with the proposal.",
    ]
    outstanding = []

    if overturned:
        # It carried and was taken back, which is neither of the other two
        # rulings and must not be reported as either. What the reversal
        # itself could not undo is on the struck decision in the record;
        # this is the standing answer to "what happened to No. 12".
        done.append("It carried, and the house vetoed it inside its window.")
        return {
            "bill": bill["no"],
            "title": bill.get("title", ""),
            "ruling": "vetoed",
            "tally": bill.get("tally_line", ""),
            "act": bill.get("act"),
            "done": done,
            "outstanding": bill.get("outstanding", []),
        }

    if passed:
        if veto_open(bill):
            done.append(
                "It carried, and the window to take it back is still open: "
                "anyone who could have voted on it may veto it until then."
            )
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
                    "This decision changes the shape of the server, which "
                    "Eugene cannot do himself yet. Someone has to make the "
                    "change in Discord by hand."
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
        "tally": bill.get("tally_line", ""),
        "act": bill.get("act"),
        "done": done,
        "outstanding": outstanding,
    }


async def post_closing_report(guild, bill):
    """The standing item after every close, so a passed decision never looks
    finished when it is not.

    Only what is still wanted reaches the record, and it goes onto the
    decision's own card. The rest of the report -- the vote closed, the
    ballots destroyed, the notes sealed -- is true of every close there has
    ever been, and a line that is on every decision is a line nobody reads.
    Clarence still gets all of it to say once, out loud, at the time.
    """
    report = closing_report(bill)
    if report["outstanding"]:
        bill["outstanding"] = report["outstanding"]
        await paint_record(guild, bill)
        await update_bill(guild, bill)
    return report


# ---------- the private word after an invitation carries ----------
# The one message Eugene sends that nobody else ever sees, and the one a
# house is most likely to want in its own words: it is the first thing a
# newcomer is shown, second hand, by whoever forwards it. The shipped
# sentence is his voice, and a house that has taught him to sound like
# something else in `voice` should not have this one paragraph still
# speaking in his.
INVITE_DM = "invite_dm"

DEFAULT_INVITE_DM = (
    "{proposer}: the cooperative has approved your invitation of {name} "
    "(Proposal No. {number}). One link, single use, seven days: {link}"
)

# What a house may write into one. `{name}` is the invitee because that is
# what somebody reaches for first; `{invitee}` is the same thing said
# plainly, for anyone who reads `{name}` as the person being written to.
INVITE_DM_FIELDS = {
    "name": "who the invitation is for",
    "invitee": "the same, said plainly",
    "proposer": "who proposed them, and who this is sent to",
    "server": "the name of this server",
    "number": "the proposal number it passed under",
    "link": "the invite link itself",
}

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def fill(template, values):
    """Substitute what is known and leave alone what is not.

    Not `str.format`: a steward who writes `{their name}` or an unmatched
    brace would get a KeyError or a ValueError, and the cost of that is
    not a bad message -- it is a passed invitation whose link never
    arrives. Anything off the list comes back on the page as typed, which
    is at least visible and fixable.
    """
    return _PLACEHOLDER.sub(
        lambda m: str(values.get(m.group(1), m.group(0))), template
    )


def invitee_name(bill):
    """Who an invitation was for. Filed on the proposal since this landed;
    read back off the title for the ones already on the floor, which both
    filing routes build the same way."""
    named = (bill.get("invitee") or "").strip()
    if named:
        return named
    title = (bill.get("title") or "").strip()
    prefix = "Invitation of "
    if title.startswith(prefix):
        return title[len(prefix):].strip() or "them"
    return "them"


def invite_dm(guild, bill, proposer, url):
    """The message the proposer gets, in the house's words if it has any."""
    template = settings.get(guild.id, INVITE_DM) or DEFAULT_INVITE_DM
    name = invitee_name(bill)
    text = fill(template, {
        "name": name,
        "invitee": name,
        "proposer": getattr(proposer, "display_name", "you"),
        "server": guild.name,
        "number": bill.get("no"),
        "link": url,
    }).strip()
    # The link is the whole point of the message, so a template that
    # forgot it -- or spent its one mention on a placeholder that does not
    # exist -- gets it on the end rather than sending somebody a
    # congratulation they cannot use.
    if url and url not in text:
        text = f"{text}\n{url}".strip()
    return text[:2000]


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
    # The code, never the url: it is what a veto needs to find this link
    # again among the server's, and it is not the link itself, so a record
    # that leaks is not a door that opens.
    bill["invite_code"] = invite.code
    await update_bill(guild, bill)
    try:
        await proposer.send(invite_dm(guild, bill, proposer, invite.url))
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
    """A passed removal proposal: private farewell, role withdrawn, removal."""
    member = guild.get_member(bill.get("target_id"))
    if member is None:
        return await health_log(
            guild, f"Proposal No. {bill['no']}: the subject had already left."
        )
    try:
        # The count goes on the decision in the record, where the house can
        # read it. It does not go in here. Somebody being shown the door is
        # owed the fact and not the arithmetic of it, and they cannot reach
        # the record to be argued with anyway. What is said here is only
        # what was never in doubt: nobody ever sees an individual ballot.
        await member.send(
            f"{guild.name} has voted for your removal. How any one person "
            f"voted is not recorded anywhere and never was. Eugene wishes "
            f"you well."
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
    # The same sums the ballot has been showing all along. They used to be
    # counted again here, which is how a close can quietly rule by a rule
    # the line under the buttons never mentioned.
    st = vote_state(guild, bill)
    yes, no, abstain = st["yes"], st["no"], st["abstain"]
    bill["tally"] = {"yes": yes, "no": no}
    if bill.get("kind") == "invite":
        # Recorded, and subtracted from the count only where the house has
        # asked for that. Out of the box a threshold is a share of the
        # roster, so an abstention neither carries the door nor lowers what
        # carries it: it is a seat that showed up and said nothing, and it
        # lands where silence lands. Printed either way, because "4 yes of
        # 7, and 2 of the rest were present" is a different story from "4
        # yes of 7, and 3 said no".
        bill["tally"]["abstain"] = abstain

    if bill.get("kind") == "kick":
        bill["threshold"] = {"eligible": st["counted"], "required": st["need"]}
        passed = yes >= st["need"]
        # The bar goes on the line with the count, the same as every other
        # close. A removal's threshold is not a share of anything, so
        # without it printed there is no reading 2 / 3 correctly.
        line = f"✅ {yes} / ❌ {no} · needed {st['need']} of {st['counted']}"
        await finalize_bill(guild, bill, passed, line)
        if passed:
            await execute_kick(guild, bill)
        return

    line = f"✅ {yes} / ❌ {no}"
    if bill.get("kind") == "invite":
        line += f" / 🤍 {abstain}"
    if st["size"] > 0:
        # `counted` beside the roster, because they are the same number
        # only while the house counts against the roster, and a record that
        # says "needed 3 of 5" a year from now has to say which five.
        bill["threshold"] = {
            "roster": st["size"], "counted": st["counted"],
            "required": st["need"], "tier": st["tier"],
        }
        passed = yes >= st["need"]
        line += f" · needed {st['need']} of {st['counted']}"
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
    defend itself.

    None of that is the house's to set, and the counting switches do not
    reach it. A choice ballot is already decided among the votes cast, and
    a share above a majority here would not raise a bar -- it would only
    send every ballot to a runoff that a plurality settles anyway, which is
    a threshold that reads as strict and binds on nothing."""
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
    ends = now_utc() + timedelta(hours=numbers(guild)["floor_hours"])
    bill["ends_at"] = ends.isoformat()
    # A runoff is a fresh vote wearing the same number: it keeps its filing
    # date, so anything measuring how far through the window we are has to
    # measure from here instead.
    bill["round_opened_at"] = now_utc().isoformat()

    # A runoff is the same vote, so it is the same card: new buttons, new
    # clock, and a line saying why. It used to close the old ballot, post a
    # card explaining the runoff and post a second ballot under it, which
    # is three messages to say that a vote carried on.
    bill["runoff_note"] = (f"No option won a majority ({tally_line}). The "
                           f"vote reopens with the leading options; regroup "
                           f"around what can win.")
    await paint_floor(guild, bill)

    await update_bill(guild, bill)
    log.info(f"runoff opened: proposal no. {bill['no']} ({tally_line})")


# ---------- asking the whole server ----------
# The one thing he runs that reaches past the cooperative, and it is built
# to be unable to reach back. A poll has its own store, its own room and
# its own close; it earns no number, touches no record, and none of the
# functions above are asked about one. That separation is the feature. The
# audience split was taken out of this file once for being threaded through
# a single pipeline, and the way to have it back without having that back
# is for a poll to share nothing with a proposal but the building.
#
# What that costs is a second set of small functions down here that look a
# little like the ones above. That is the price, it is paid on purpose, and
# it is cheaper than one `if audience ==` in `close_bill`.

POLL_WHAT = 400
POLL_WHY = 200


def polls_room(guild):
    """The room community polls go in, or None if the house has not bound
    one. Never falls back to the floor: a poll in the cooperative's room is
    the exact confusion this feature is shaped to avoid."""
    return room(guild, "polls")


def poll_room_note(guild):
    """Why a poll cannot go up here, or None when it can."""
    if not module_live(guild, "polls"):
        return module_note(guild, "polls")
    if polls_room(guild) is None:
        return ("There is no room bound for the `polls` job, so a community "
                "poll has nowhere to go. An admin can point me at one with "
                "`/setup`.")
    return None


def poll_audience(guild):
    """How many people a poll is being put to.

    Everybody here who is not a bot, and no other test. Not the roll, not
    who is awake, not who has a role: the quorum is a share of the room,
    and the room is the room. Away is a cooperative idea and has no meaning
    for somebody who never signed up to be counted in the first place.
    """
    if guild is None:
        return 0
    return sum(1 for m in guild.members if not m.bot)


def poll_figures(guild):
    held = numbers(guild)
    return held["poll_share"], held["poll_quorum_share"], held["poll_hours"]


def poll_quorum(guild):
    _share, quorum_share, _hours = poll_figures(guild)
    return polls.quorum(poll_audience(guild), quorum_share)


def poll_text(poll):
    """The question as the card shows it, and the whole of it when that is
    less. Same bargain the floor makes: a room of polls is a list, and a
    question nobody scrolls past is a question nobody answers."""
    question = (poll.get("question") or "").strip()
    why = (poll.get("why") or "").strip()
    shown_q, cut_q = clip(question, POLL_WHAT)
    shown_why, cut_why = clip(why, POLL_WHY)
    lines = [line for line in (f"### {shown_q}",
                               f"-# {shown_why}" if shown_why else "") if line]
    return "\n".join(lines), (cut_q or cut_why)


def poll_standing(guild, poll):
    """Where an open poll is, in one line: turnout against the quorum, what
    has been said, and when it shuts.

    The quorum is on the line because it is the only number that decides
    whether any of this gets reported, and a poll four answers short of
    counting looks identical to one that is fine without it.
    """
    share, quorum_share, _hours = poll_figures(guild)
    size = poll_audience(guild)
    tally = polls.counts(poll)
    voted = sum(tally.values())
    floor = polls.quorum(size, quorum_share)
    said = " · ".join(
        f"{'🤍 ' if name == polls.ABSTAIN else ''}{name} {count}"
        for name, count in tally.items() if name != polls.ABSTAIN or count
    )
    parts = [f"`{bar(voted, floor)}` {voted} of {size} answered"]
    parts.append("enough to report" if voted >= floor
                 else f"{floor - voted} more to report")
    if said:
        parts.append(said)
    ends = poll.get("ends_at")
    if ends:
        try:
            when = int(datetime.fromisoformat(ends).timestamp())
            parts.append(f"closes <t:{when}:R>")
        except (TypeError, ValueError):
            # A card that will not draw is a poll nobody can answer. The
            # closing time is the one thing on this line that is worth less
            # than the line itself, so it is the one thing that goes.
            log.warning(f"poll {poll.get('id')} has an unreadable ends_at")
    return " · ".join(parts)


def poll_verdict(guild, poll):
    """What a closed poll came to, in the words it is reported in.

    Never "passed" and never "carried". Those words belong to the
    cooperative and mean somebody has to go and do something; a poll means
    the room was asked and this is what it said.
    """
    result = poll.get("result") or {}
    tally = result.get("counts") or {}
    said = " · ".join(f"{name} {count}" for name, count in tally.items()
                      if name != polls.ABSTAIN or count)
    if not result.get("quorate"):
        head = (f"**No result.** {result.get('voted', 0)} of "
                f"{result.get('room', 0)} answered, and "
                f"{result.get('quorum', 0)} were needed for this to report "
                f"anything.")
        # The split goes under it even here. It is not a result and must
        # not read as one, but a poll that fell four short is worth
        # knowing about, and hiding the numbers would only mean somebody
        # asks the room the same question again next week.
        return f"{head}\n-# {said}" if said else head
    answer = result.get("answer")
    if answer is None:
        leaders = result.get("leaders") or []
        head = (f"**Tied**, between {' and '.join(leaders)}."
                if leaders else "**No answer.** Nobody chose anything.")
    else:
        head = f"**The room said: {answer}.**"
    tail = f"{result['voted']} of {result['room']} answered · {said}"
    if result.get("needed"):
        tail += f" · {result['needed']} of those who chose carried it"
    return f"{head}\n-# {tail}"


def poll_segments(guild, poll):
    """The whole card, open or closed. One message per poll from the moment
    it goes up to the result on it, redrawn and never added to."""
    body, _cut = poll_text(poll)
    who = poll.get("author") or "somebody"
    if poll.get("status") == polls.CLOSED:
        return [body, f"-# Asked by {who}. Closed.",
                poll_verdict(guild, poll)]
    return [
        body,
        f"-# Asked by {who}. Anyone here can answer, answers are "
        f"anonymous, and nothing here binds anybody.",
        poll_standing(guild, poll),
    ]


async def cast_poll_answer(interaction, choice=None, index=None):
    """Record one answer on a community poll, or retract it.

    Everyone in the server may answer and that is the whole of the gate --
    but it is still a gate, and it is asked here rather than left to the
    channel's permissions, for the same reason `may_vote` is: a card that
    survived a restart or a room whose overwrites drifted must not become a
    way into something.
    """
    guild = interaction.guild
    poll = polls.by_field(guild.id, "message_id", interaction.message.id)
    if poll is None or poll.get("status") != polls.OPEN:
        return await interaction.response.send_message(
            "This poll has closed.", ephemeral=True
        )
    if getattr(interaction.user, "bot", False):
        return await interaction.response.send_message(
            "Bots do not answer polls.", ephemeral=True
        )
    if index is not None:
        options = poll.get("options") or []
        if index >= len(options):
            return await interaction.response.send_message(
                "That answer is not on this poll.", ephemeral=True
            )
        choice = options[index]
    if choice is not None and not polls.may_answer(poll, choice):
        return await interaction.response.send_message(
            "That answer is not on this poll.", ephemeral=True
        )
    async with _state_lock:
        fresh = polls.by_id(guild.id, poll["id"])
        if fresh is None or fresh.get("status") != polls.OPEN:
            return await interaction.response.send_message(
                "This poll has closed.", ephemeral=True
            )
        moved = polls.cast(fresh, interaction.user.id, choice)
        if moved:
            polls.put(guild.id, fresh)
        poll = fresh
    if choice is None:
        await interaction.response.send_message(
            "Answer withdrawn." if moved else "You have no answer to withdraw.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"Your answer: **{choice}**. You can change it until the poll "
            f"closes, and nobody ever sees how you answered.",
            ephemeral=True,
        )
    if moved:
        await paint_poll(guild, poll)


class PollRow(discord.ui.ActionRow):
    """A yes/no poll. Abstain is on it because turning up to say nothing is
    what tells a quorum apart from a room that was not listening."""

    @discord.ui.button(
        label="Yes", emoji="✅",
        style=discord.ButtonStyle.success, custom_id="clerk:poll_yes",
    )
    async def yes(self, interaction, button):
        await cast_poll_answer(interaction, polls.YES)

    @discord.ui.button(
        label="No", emoji="❌",
        style=discord.ButtonStyle.secondary, custom_id="clerk:poll_no",
    )
    async def no(self, interaction, button):
        await cast_poll_answer(interaction, polls.NO)

    @discord.ui.button(
        label="No view", emoji="🤍",
        style=discord.ButtonStyle.secondary, custom_id="clerk:poll_abstain",
    )
    async def abstain(self, interaction, button):
        await cast_poll_answer(interaction, polls.ABSTAIN)

    @discord.ui.button(
        label="Withdraw", style=discord.ButtonStyle.secondary,
        custom_id="clerk:poll_retract",
    )
    async def retract(self, interaction, button):
        await cast_poll_answer(interaction)


class PollChoiceRows(list):
    """A choice poll: one button per answer, and the same two greys on the
    end. A bare instance with dummy labels routes presses after a restart,
    exactly as `MultiBallotRows` does for a ballot."""

    def __init__(self, options=None):
        super().__init__()
        labels = options if options is not None else [
            f"Option {i + 1}" for i in range(polls.MAX_OPTIONS)
        ]
        row = discord.ui.ActionRow()
        for i, label in enumerate(labels[:polls.MAX_OPTIONS]):
            if len(row.children) == 5:
                self.append(row)
                row = discord.ui.ActionRow()
            button = discord.ui.Button(
                label=str(label)[:80],
                style=discord.ButtonStyle.primary,
                custom_id=f"clerk:pollopt_{i}",
            )
            button.callback = self._make_callback(i)
            row.add_item(button)
        for label, emoji, custom_id, handler in (
            ("No view", "🤍", "clerk:pollopt_abstain", self._abstain),
            ("Withdraw", None, "clerk:pollopt_retract", self._retract),
        ):
            if len(row.children) == 5:
                self.append(row)
                row = discord.ui.ActionRow()
            button = discord.ui.Button(
                label=label, emoji=emoji,
                style=discord.ButtonStyle.secondary, custom_id=custom_id,
            )
            button.callback = handler
            row.add_item(button)
        self.append(row)

    @property
    def children(self):
        return [button for row in self for button in row.children]

    def _make_callback(self, index):
        async def callback(interaction):
            await cast_poll_answer(interaction, index=index)
        return callback

    async def _abstain(self, interaction):
        await cast_poll_answer(interaction, polls.ABSTAIN)

    async def _retract(self, interaction):
        await cast_poll_answer(interaction)


def poll_rows(poll):
    if poll.get("status") == polls.CLOSED:
        # A closed poll keeps its card and loses its buttons. Leaving them
        # there is a room full of things that look answerable.
        return []
    if poll.get("options"):
        return PollChoiceRows(poll["options"])
    return [PollRow()]


async def paint_poll(guild, poll):
    """Redraw a poll's card where it stands.

    A card somebody deleted is not rebuilt. A proposal is the cooperative's
    business and gets reposted; a poll that somebody with Manage Messages
    removed from a public room has been taken down, and putting it back is
    arguing with them.
    """
    channel = guild.get_channel(poll.get("channel_id") or 0) or polls_room(guild)
    if channel is None or not poll.get("message_id"):
        return
    try:
        message = await channel.fetch_message(poll["message_id"])
    except (discord.NotFound, discord.Forbidden):
        return
    except discord.HTTPException as e:
        log.warning(f"could not fetch poll {poll.get('id')}: {e!r}")
        return
    try:
        await message.edit(
            view=Card(poll_segments(guild, poll), poll_rows(poll)),
            allowed_mentions=ring(),
        )
    except discord.HTTPException as e:
        log.warning(f"could not repaint poll {poll.get('id')}: {e!r}")


async def open_confirmed_poll(guild, poll):
    """Put a confirmed draft in front of the server.

    The only thing in this file that writes in the polls room, and it does
    it exactly once per poll: the close is an edit to this card and there
    is no other message. A room whose whole content is the polls somebody
    opened is one where an unread channel means a question is waiting.
    """
    channel = polls_room(guild)
    if channel is None:
        return None
    _share, _quorum_share, hours = poll_figures(guild)
    async with _state_lock:
        fresh = polls.by_id(guild.id, poll["id"]) if poll.get("id") else None
        if fresh is not None and fresh.get("status") != polls.DRAFT:
            # Two presses on one confirmation. The second must not open a
            # second poll, and must not be told the first one failed.
            return fresh
        poll = fresh or poll
        poll["status"] = polls.OPEN
        polls.put(guild.id, poll)
    # Sent before the window is stamped so a poll that cannot be posted --
    # no permissions, a deleted room -- never becomes an open poll nobody
    # can answer. Its status goes back if this throws.
    try:
        card = await channel.send(
            view=Card(poll_segments(guild, poll), poll_rows(poll)),
            allowed_mentions=ring(),
        )
    except discord.HTTPException as e:
        log.warning(f"could not post poll {poll.get('id')}: {e!r}")
        async with _state_lock:
            poll["status"] = polls.DRAFT
            polls.put(guild.id, poll)
        return None
    async with _state_lock:
        polls.open_poll(poll, poll["id"], hours, channel.id, card.id)
        polls.put(guild.id, poll)
    # Painted once more so the card carries its own closing time, which is
    # only known once the window has been stamped on it.
    await paint_poll(guild, poll)
    log.info(f"community poll opened: no. {poll['id']} by {poll.get('author')}")
    return poll


async def close_poll(guild, poll):
    """Count a poll, seal it, and say what the room said on its own card."""
    share, quorum_share, _hours = poll_figures(guild)
    async with _state_lock:
        fresh = polls.by_id(guild.id, poll["id"])
        if fresh is None or fresh.get("status") != polls.OPEN:
            return None
        result = polls.decided(fresh, poll_audience(guild), share, quorum_share)
        polls.close(fresh, result)
        polls.put(guild.id, fresh)
        poll = fresh
    await paint_poll(guild, poll)
    log.info(f"community poll closed: no. {poll['id']} "
             f"({result.get('answer')!r}, {result['voted']}/{result['room']})")
    return poll


@tasks.loop(seconds=60)
async def check_polls():
    """Shut the polls whose windows have run out, and sweep the drafts
    nobody confirmed. Its own loop rather than a branch inside the floor's,
    so a house with polls off runs none of it and a poll that throws cannot
    stop a proposal closing."""
    for guild in houses():
        try:
            if not module_live(guild, "polls"):
                continue
            polls.sweep(guild.id)
            for poll in polls.due(guild.id):
                try:
                    await close_poll(guild, poll)
                except Exception as e:
                    log.error(f"failed to close poll {poll.get('id')}: {e!r}")
        except Exception as e:
            log.error(f"the poll check failed in {guild.id}: {e!r}")


# ---------- agreeing to put one up ----------
# A proposal is filed the moment somebody says what they want, with no
# confirmation step, because it goes to eight people who all signed up to
# read it. This one does not. It goes in front of everybody in the
# building, most of whom never asked to be asked anything, and the person
# opening it should have seen that sentence before it happens rather than
# after. So it is the one thing in here that is agreed to twice, and the
# card below is where the second time happens -- in the place it was asked
# for, whether that is a slash command or a conversation.

def poll_preview(guild, poll):
    """What they are agreeing to, with the numbers filled in.

    The audience is a count and not a word, because "everyone" reads as an
    abstraction and "142 people" does not, and the difference between the
    two is the whole reason this card exists.
    """
    size = poll_audience(guild)
    floor = poll_quorum(guild)
    _share, _quorum_share, hours = poll_figures(guild)
    inside = len(cooperative_members(guild))
    answers = poll.get("options") or [polls.YES, polls.NO]
    body, _cut = poll_text(poll)
    return "\n".join([
        f"## This goes to the whole of {guild.name}",
        f"Everyone here can see it and answer it: **{size} people**, not "
        f"the {inside} on the roll. It decides nothing. No proposal is "
        f"filed, nothing reaches the record, and nobody is bound by the "
        f"answer.",
        "",
        body,
        f"-# Answers: {' · '.join(answers)} · open {hours:g}h · "
        f"{floor} answers needed before anything is reported",
    ])


class PollConfirm(discord.ui.View):
    """The second yes. Persistent, because the conversational half of this
    is a real message in a real room, and a deploy between asking and
    agreeing must not leave a live button with nothing behind it."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _draft(self, interaction):
        """The draft this card belongs to, having checked who is pressing.

        Three questions, and all three are asked on the press rather than
        trusted from when the card went up: it is still a draft, the
        presser is the person who asked for it, and they are still in the
        cooperative. The last one matters because a card can sit for an
        hour, and a poll opened in somebody's name after they left the roll
        is a poll nobody is answerable for.
        """
        guild = interaction.guild
        poll = polls.by_field(guild.id, "draft_message_id", interaction.message.id)
        if poll is None or poll.get("status") != polls.DRAFT:
            await interaction.response.edit_message(
                content="That draft is no longer waiting.", view=None
            )
            return None
        if interaction.user.id != poll.get("author_id"):
            await interaction.response.send_message(
                "This one is not yours to put up. Whoever asked for it "
                "agrees to it.", ephemeral=True,
            )
            return None
        if not in_cooperative(interaction.user):
            await interaction.response.send_message(
                "A community poll is opened by the cooperative, and you are "
                "not on the roll any more.", ephemeral=True,
            )
            return None
        return poll

    @discord.ui.button(
        label="Put it to the server", style=discord.ButtonStyle.primary,
        custom_id="clerk:poll_confirm",
    )
    async def confirm(self, interaction, button):
        poll = await self._draft(interaction)
        if poll is None:
            return
        note = poll_room_note(interaction.guild)
        if note:
            return await interaction.response.edit_message(
                content=note, view=None
            )
        await interaction.response.edit_message(
            content="Putting it up…", view=None
        )
        opened = await open_confirmed_poll(interaction.guild, poll)
        if opened is None:
            return await interaction.edit_original_response(
                content="I could not post it. Check I can write in the "
                        "polls room.",
            )
        where = polls_room(interaction.guild)
        await interaction.edit_original_response(
            content=f"It is up in {where.mention}, open to everyone here.",
        )

    @discord.ui.button(
        label="Discard", style=discord.ButtonStyle.secondary,
        custom_id="clerk:poll_discard",
    )
    async def discard(self, interaction, button):
        poll = await self._draft(interaction)
        if poll is None:
            return
        async with _state_lock:
            kept = [p for p in polls.load(interaction.guild.id)
                    if p.get("id") != poll.get("id")]
            polls.save(interaction.guild.id, kept)
        await interaction.response.edit_message(
            content="Discarded. Nothing was posted.", view=None
        )


async def offer_poll(guild, author, question, why=None, options=None):
    """Write a draft down and return it, ready to be confirmed.

    Stored rather than held in memory so that the hour a draft may sit for
    survives a redeploy: what is cheap to rebuild is the card, and what is
    not is the question somebody typed.
    """
    async with _state_lock:
        poll = polls.draft(author.id, author.display_name, question, why, options)
        poll["id"] = polls.next_id(guild.id)
        polls.put(guild.id, poll)
    return poll


async def send_poll_confirm(guild, poll, send):
    """Put the confirmation card up through whichever door asked for it,
    and remember which message it is so the button can find its draft.

    `send` is the one difference between the two doors -- an ephemeral
    reply to a slash command, a message in the room for a conversation --
    and it returns the message it sent.
    """
    message = await send(poll_preview(guild, poll), PollConfirm())
    if message is not None:
        async with _state_lock:
            fresh = polls.by_id(guild.id, poll["id"]) or poll
            fresh["draft_message_id"] = message.id
            polls.put(guild.id, fresh)
    return message


class PollModal(discord.ui.Modal, title="Ask the whole server"):
    """The form. It says who it is going to before the boxes, because the
    confirmation after it is a second chance to notice and this is the
    first."""

    question = discord.ui.TextInput(
        label="The question, as the server will read it",
        style=discord.TextStyle.paragraph,
        max_length=500,
        placeholder="Should we move game night to Saturdays?",
    )
    why = discord.ui.TextInput(
        label="Any context. Blank is fine.",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=400,
    )
    choices = discord.ui.TextInput(
        label="Answers, one per line. Blank for yes/no.",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=400,
        placeholder="Saturday\nSunday\nLeave it where it is",
    )

    async def on_submit(self, interaction):
        options = polls.clean_options(str(self.choices).splitlines())
        refusal = polls.options_refusal(options)
        if refusal:
            return await interaction.response.send_message(refusal, ephemeral=True)
        poll = await offer_poll(
            interaction.guild, interaction.user,
            str(self.question).strip(), str(self.why).strip() or None, options,
        )

        async def send(content, view):
            await interaction.response.send_message(
                content, view=view, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return await interaction.original_response()

        await send_poll_confirm(interaction.guild, poll, send)


@tasks.loop(seconds=60)
async def check_floor():
    for guild in houses():
        try:
            await _check_floor_in(guild)
        except Exception as e:
            # One house's clock must not stop every other house's.
            log.error(f"the floor check failed in {guild.id}: {e!r}")


async def _check_floor_in(guild):
    if not module_live(guild, "governance"):
        return
    for bill in load_json(bills_path(guild), []):
        # A window that has run out is shut on the same clock the vote
        # closes on. Left open, its buttons keep working, and a veto that
        # arrives a week late is the one thing the window exists to stop.
        veto = bill.get("veto")
        if veto and not veto.get("closed") and not veto_open(bill):
            try:
                await close_veto(guild, bill)
            except Exception as e:
                log.error(f"failed to close the veto on bill {bill['no']}: {e!r}")
            continue
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


def is_priority(bill):
    """Whether a vote is worth a private word to whoever has not voted.

    A DM is the most intrusive thing in the building, and a bot that sends
    one about every proposal teaches people to ignore all of them --
    including the one that mattered. So the nudge is rationed to two kinds,
    and everything else is left to the ballot in the room to say for
    itself:

    - anything at the fundamental tier: a removal, a rule change, a change
      to how voting works. Silence is a no, and a no there is a decision
      about somebody's standing or about the rules everyone lives under.
    - anything filed as priority, which is a claim the author makes in the
      open and which everyone can see on the proposal.

    """
    return vote_tier(bill) == "fundamental" or bool(bill.get("priority"))


def nudge_roll(guild, bill):
    """Who is worth a private word about a vote they have not cast: the
    roll the ballot counts against, so nobody is nudged about a vote they
    could not cast -- and nobody at all unless the vote is a priority
    one."""
    if not is_priority(bill):
        return []
    return electorate(guild, bill)


def ballot_link(guild, bill):
    floor = floor_for(guild, bill)
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

    What silence costs depends on the vote, and on how this house counts.
    A yes/no ballot carried against the whole roster makes not voting as
    good as a no. A choice ballot is settled among the votes cast, so
    silence is not a vote against anything -- it just hands the answer to
    whoever did turn up -- and a house counting against turnout has said
    the same of every vote it holds. Saying otherwise would be a lie told
    to get somebody to click, which is the one thing a nudge cannot be.
    """
    st = vote_state(guild, bill)
    if st["options"]:
        cost = "and the answer is being chosen without you"
    elif is_blind(bill):
        cost = "and the house is still short of a view"
    elif numbers(guild)[roster.TURNOUT]:
        cost = "and it is being settled among whoever does vote"
    else:
        cost = "and silence counts against it"
    try:
        closes = f" Closes <t:{int(datetime.fromisoformat(bill['ends_at']).timestamp())}:R>."
    except (KeyError, ValueError):
        closes = ""
    return (
        f"**Proposal No. {bill['no']}: {bill['title']}**\n"
        f"You have not voted, {cost}. {standing_line(guild, bill)}"
        f"{closes}{ballot_link(guild, bill)}\n"
        f"-# Tell me to stop nudging and I will."
    )


async def send_nudges(guild, silent=False):
    """A quiet word to whoever has not voted, once per vote, halfway through.

    Sent privately on purpose: who has and has not voted is not something to
    put in a channel, and a nudge in public is a shaming.
    """
    bills = load_json(bills_path(guild), [])
    for bill, user_id in duties.nudges_due(guild.id, bills,
                                            lambda b: nudge_roll(guild, b)):
        member = guild.get_member(user_id)
        if member is not None and not silent:
            try:
                await member.send(nudge_text(guild, bill))
                log.info(f"nudged {member.display_name} about proposal {bill['no']}")
            except discord.HTTPException as e:
                log.info(f"could not nudge {member.display_name}: {e!r}")
        # Written down either way. A door that will not open is not one to
        # keep knocking on every quarter of an hour.
        duties.mark_said(guild.id, duties.nudge_key(bill, user_id))


# Two of them, because the whole point of Away is what it costs you, and
# that depends on how the house counts. Told the roster version in a house
# counting turnout, somebody would be reassured about a thing that was
# never happening to them.
AWAY_GONE = (
    "A fortnight quiet, so I have taken you off the roster for now. Nobody "
    "thinks anything of it and nothing you did is undone: it only means "
    "votes stop counting your silence as a no. Say anything here and you are "
    "straight back on."
)

AWAY_GONE_TURNOUT = (
    "A fortnight quiet, so I have taken you off the roster for now. Nobody "
    "thinks anything of it and nothing you did is undone: votes here are "
    "counted among whoever casts one, so it only means you are not one of "
    "the number they are measured against. Say anything here and you are "
    "straight back on."
)

AWAY_BACK = "You are back on the roster. Votes count you again."


async def tell_away(guild, silent=False):
    """The rules of procedure promise that Eugene marks people Away and tells
    them he did. He was doing the first half."""
    members = [m for m in guild.members if not m.bot and in_cooperative(m)]
    gone, back, quiet_now = duties.away_changes(
        guild.id, members, lambda m: roster.away_reason(guild.id, m))
    stepped_out = (AWAY_GONE_TURNOUT if numbers(guild)[roster.TURNOUT]
                   else AWAY_GONE)
    told = [(m, stepped_out) for m in gone] + [(m, AWAY_BACK) for m in back]
    if not silent:
        for member, line in told:
            if duties.muted(guild.id, member.id):
                continue
            try:
                await member.send(line)
            except discord.HTTPException as e:
                log.info(f"could not tell {member.display_name} about the roster: {e!r}")
    if told:
        log.info(f"roster: {len(gone)} gone quiet, {len(back)} back")
    duties.record_quiet(guild.id, quiet_now)


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
    items = duties.outstanding(load_json(bills_path(guild), []), closing_report)
    content = outstanding_content(items)
    state = load_json(state_path(guild), {})
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
        state = load_json(state_path(guild), {})
        state["outstanding_message_id"] = message.id
        save_json(state_path(guild), state)
    elif message.content != content:
        await message.edit(content=content)
    # The list is silent; once a week, if it is not empty, one line points at
    # it. That is the whole of the chasing.
    if items and not silent and duties.chase_due(guild.id):
        duties.mark_chased(guild.id)
        await channel.send(
            f"-# {len(items)} decision(s) still want doing. The list is pinned."
        )
    return items


@tasks.loop(minutes=DUTY_MINUTES)
async def duty_loop():
    for guild in houses():
        try:
            await _duties_in(guild)
        except Exception as e:
            log.error(f"the duty round failed in {guild.id}: {e!r}")


async def _duties_in(guild):
    # The first round against a fresh ledger only takes stock. Everything
    # already true when this was switched on is history, not news, and a
    # server should not be woken up by a fortnight of it at once.
    if not module_live(guild, "governance"):
        return
    settling = duties.opening_pass(guild.id)
    for duty in (send_nudges, tell_away, update_outstanding):
        try:
            await duty(guild, silent=settling)
        except Exception as e:
            # One duty failing must not take the other two down with it.
            log.error(f"duty {duty.__name__} failed: {e!r}")
            await health_log(guild, f"⚠️ `{duty.__name__}` failed: `{e!r}`")
    if settling:
        duties.mark_started(guild.id)
        log.info("duty ledger opened; from here on he speaks up")


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
    priority = bool(args.get("priority"))
    bill = await file_bill(guild, invoker, title=title, what=what, why=why,
                           priority=priority)
    if bill is None:
        return json.dumps({"error": "the votes channel is missing"})
    return json.dumps({"filed": bill["no"], "title": title,
                       "author": bill["author"], "closes_at": bill["ends_at"],
                       "priority": priority,
                       "note": ("Everyone who has not voted will get one "
                                "direct message about it, halfway through."
                                if priority else
                                "Nobody is direct-messaged about an ordinary "
                                "proposal; the ballot in the room speaks for "
                                "itself.")})


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
    bill = await file_bill(
        guild, invoker, title=f"Invitation of {name}"[:100],
        what=invite_what(guild, name, discord_id), why=why,
        kind="invite", invitee=name,
        target_id=int(discord_id) if discord_id else None,
    )
    if bill is None:
        return json.dumps({"error": "the votes channel is missing"})
    return json.dumps({"filed": bill["no"], "proposed": name,
                       "author": bill["author"], "closes_at": bill["ends_at"],
                       "ballot": "yes, no, or abstain; anonymous; no running "
                                 "count while it is open, tally published at close"})


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
    bill = bill_by(guild, "no", bill_no)
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

    # Said on the ballot the close lands on rather than in a card of its
    # own: whoever the early close cut off is looking at the ballot.
    bill["closed_early_by"] = getattr(invoker, "display_name", "somebody")
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


async def act_propose_removal(guild, invoker, args):
    """The route that opens when the officer's tools close.

    Someone asks Eugene to kick a member of the cooperative; the hands
    refuse, because that is a fundamental vote. This is what they are
    refused *towards*, so the answer is "filed, No. 14, here is what it
    needs" rather than "no". Every check the button makes is made here
    too -- same guards, same wording, one route or the other.
    """
    target, why_not = powers.find_member(guild, args.get("who"))
    if target is None:
        return json.dumps({"error": why_not})
    if not in_cooperative(target):
        return json.dumps({"error": f"{target.display_name} is not in the "
                                    f"cooperative, so there is nothing to "
                                    f"remove. That one is an ordinary kick."})
    if target.bot:
        return json.dumps({"error": "the machines are not subject to removal"})
    if target.id == guild.owner_id:
        return json.dumps({"error": "Discord will not let the server owner be "
                                    "removed; ownership is a different matter"})
    if target.id == invoker.id:
        return json.dumps({"error": "they may simply leave; the door works in "
                                    "both directions"})
    for b in load_json(bills_path(guild), []):
        if (b.get("kind") == "kick" and b.get("target_id") == target.id
                and b.get("status") == "on_floor"):
            return json.dumps({"error": f"already up for a vote, No. {b['no']}"})
    why = _clean(args.get("why"), 4000)
    if not why:
        return json.dumps({"error": "a removal without reasons is not a proposal"})

    eligible = [m for m in guild.members
                if in_cooperative(m) and not m.bot and m.id != target.id]
    bill = await file_bill(
        guild, invoker, title=f"Removal of {target.display_name}"[:100],
        what=removal_what(guild, target.display_name), why=why,
        kind="kick", target_id=target.id,
        floor_hours=numbers(guild)["removal_hours"],
        eligible_ids=[m.id for m in eligible],
    )
    if bill is None:
        return json.dumps({"error": "the votes channel is missing"})
    return json.dumps({"filed": bill["no"], "about": target.display_name,
                       "author": bill["author"], "closes_at": bill["ends_at"],
                       "ballot": "anonymous; the tally is never published",
                       "note": "The subject keeps the whole window to answer. "
                               "Nothing happens to them until it closes."})


async def act_propose(guild, invoker, args):
    """One door onto filing; the three handlers underneath are unchanged.

    `who` does double duty for an invitation and a removal, because from
    the asker's side they are the same sentence with a different verb, and
    a schema that called it `name` in one and `who` in the other is a
    schema that gets filled in wrong.
    """
    kind = str(args.get("kind") or "").strip().lower()
    if kind == "change":
        return await act_propose_bill(guild, invoker, args)
    if kind == "invite":
        return await act_propose_member(
            guild, invoker,
            {"name": args.get("who"), "discord_id": args.get("discord_id"),
             "why": args.get("why")},
        )
    if kind == "removal":
        return await act_propose_removal(guild, invoker, args)
    return json.dumps(
        {"error": f"{kind!r} is not something to propose; kind is one of "
                  f"change, invite, removal"}
    )


BILL_ACTIONS = {
    "propose": act_propose,
    "close_floor": act_close_floor,
}


# ---------- a poll, asked for in conversation ----------

async def act_open_community_poll(guild, invoker, args, context=None):
    """Draft a community poll and put the confirmation in front of whoever
    asked for it. Opens nothing.

    This is the one tool in the building that does not do the thing it is
    named after. Everything else Eugene is handed acts on the first ask --
    a proposal is filed, a setting is changed, and asking somebody to
    confirm is a small insult to a person who already said what they
    wanted. A poll is the exception because of who is on the other end of
    it: the cooperative signed up to be asked things and the other hundred
    and forty people in the server did not, and a model that misreads
    "what does everyone think" as an instruction would spend that room's
    attention on Eugene's guess. So the tool writes the question down,
    shows it to them with the audience counted, and stops.
    """
    channel = (context or {}).get("channel")
    # A room in this server, and nothing else. No channel at all means
    # there is nowhere to ask for the second yes, which is the whole of
    # what the tool is for. A direct message is worse than useless: the
    # card would go up somewhere with no guild behind it, and the button
    # would come back looking for a server it cannot see. He is reachable
    # by DM, so this is a real door and not a hypothetical one.
    if channel is None or getattr(channel, "guild", None) is None:
        return json.dumps({
            "error": "I can only offer a poll in a room in the server it "
                     "would go to. Ask me in a channel there, or use "
                     "`/poll`.",
        })
    if guild is None or channel.guild.id != guild.id:
        return json.dumps({
            "error": "That room is not in the server this poll would go to.",
        })
    if not in_cooperative(invoker):
        return json.dumps({
            "error": "A community poll is put up by the cooperative. "
                     "Answering one is open to everybody here; opening one "
                     "is not.",
        })
    note = poll_room_note(guild)
    if note:
        return json.dumps({"error": note})
    question = _clean(args.get("question"), 500)
    if not question:
        return json.dumps({"error": "a poll needs a question"})
    options = polls.clean_options(args.get("options") or None)
    refusal = polls.options_refusal(options)
    if refusal:
        return json.dumps({"error": refusal})
    poll = await offer_poll(
        guild, invoker, question, _clean(args.get("why"), 400) or None, options
    )

    async def send(content, view):
        return await channel.send(
            content, view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    try:
        await send_poll_confirm(guild, poll, send)
    except discord.HTTPException as e:
        log.warning(f"could not offer poll in {getattr(channel, 'id', '?')}: {e!r}")
        return json.dumps({"error": "I could not put the confirmation up in "
                                    "this room."})
    return json.dumps({
        "offered": "a confirmation is waiting in this room",
        "question": question,
        "answers": options or [polls.YES, polls.NO],
        "goes_to": poll_audience(guild),
        "note": "Nothing is posted until they press it themselves. Say in "
                "one line that it is there and who it would go to. Do not "
                "press it for them and do not ask again if they ignore it.",
    })


POLL_ACTIONS = {
    "open_community_poll": act_open_community_poll,
}


# ---------- the other end of the two things Eugene starts ----------
# A nudge you cannot stop is a nuisance, and a standing list of unfinished
# work with no way to say "done" is a nag. Both of these are member-tier for
# the same reason the member tools are: neither hands anybody a power they
# did not already have. Saying a decision has been carried out is a claim on
# the record, made in public, under the name of whoever made it.

async def act_set_nudges(guild, invoker, args):
    on = args.get("on")
    if isinstance(on, str):
        on = on.strip().lower() not in ("false", "no", "off", "0")
    on = True if on is None else bool(on)
    duties.set_muted(guild.id, invoker.id, on=not on)
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
    bill = bill_by(guild, "no", bill_no)
    if bill is None:
        return json.dumps({"error": f"no Proposal No. {bill_no} on record"})
    if bill.get("status") == "vetoed":
        return json.dumps(
            {"error": f"Proposal No. {bill_no} carried and was vetoed inside "
                      f"its window, so it is not a decision to carry out",
             "status": "vetoed"}
        )
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
    await update_bill(guild, bill)
    await paint_record(guild, bill)
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
# One command, one screen. This used to be twelve subcommands -- start,
# create, rooms, roles, brain, use, forget-brain, voting, house, grant,
# revoke, status -- and three of them made channels. Nobody could tell from
# the outside which to run first, what the second would do to what the
# first had built, or what the server would look like when they were done.
#
# So: `/setup` opens a panel. It reads the server, says what is done and
# what is missing, and changes nothing until somebody presses Apply. Every
# door the twelve commands opened is a button on it, and one of the buttons
# prints the layout that is about to exist, before it exists.
#
# It is for whoever runs the place, not for everyone else: shaping the
# server and paying for Eugene's thinking are not put to a vote. Every
# callback re-checks that, because a view lives on in a message and the
# person who presses a button is not always the person the panel opened
# for. A server brings its own brain key, typed into a modal and answered
# ephemerally, so it reaches no channel, no log and no other screen; the
# store keeps it 0600 and shows only its last four digits.

STEWARD_ONLY = "That one is for whoever runs the place, sorry."


def is_admin(member):
    """Whoever could already do it by hand.

    The same question the setup panel asks, pulled out of the interaction
    so the sign-off desk can ask it too. Administrator or owner and
    nothing else: not a named role, because a role can be handed out by
    anyone who holds it, and a gate whose keys can be copied by the
    people it gates is decoration.
    """
    guild = getattr(member, "guild", None)
    return isinstance(member, discord.Member) and (
        member.guild_permissions.administrator
        or (guild is not None and member.id == guild.owner_id)
    )


def is_steward(interaction):
    """Whoever could already reshape this server by hand. The command is
    hidden from everyone else, but hiding is not a boundary, so it is
    checked again here."""
    return is_admin(interaction.user)


def brain_lines(guild):
    """The state of every annex, in a form safe to put on a screen."""
    on_duty = brain.provider_name(guild.id)
    if on_duty is None:
        return [
            "Brain: **dormant**, no key. The Brain button wakes him.",
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


# ---------- what this server currently has ----------
# `modules.py` decides what a gap means; these three say what is there. The
# split keeps the rules testable on a laptop:
# and only the lookups need a server.

ROLE_LOOKUPS = {
    "cooperative": lambda guild: cooperative_role(guild),
    "member": lambda guild: member_role(guild),
    "bell": lambda guild: bell_role(guild),
}


def role_for(guild, key):
    """A role by the job it does. An unknown key is nobody rather than
    whichever role the last branch of an if happened to name: this used to
    answer `member` to anything that was not `cooperative`, which meant a
    third job would have quietly reported itself bound from the day it was
    declared."""
    lookup = ROLE_LOOKUPS.get(key)
    return lookup(guild) if lookup else None


def bound_rooms(guild, keys=None):
    """Room jobs that resolve to a channel that still exists."""
    return {k for k in (keys if keys is not None else bindings.ROOMS)
            if room(guild, k) is not None}


def bound_roles(guild, keys=None):
    return {k for k in (keys if keys is not None else bindings.ROLES)
            if role_for(guild, k) is not None}


def has_brain(guild):
    return brain.provider_name(guild.id) is not None


def _probe(guild, key):
    """What this one module needs, looked up and no more.

    Narrow on purpose. `module_live` is asked on every message, and asking
    the server about eight rooms and two roles to answer a question about a
    module that declared neither is work nobody wanted done.
    """
    spec = modules.spec(key) or {}
    return dict(
        rooms=bound_rooms(guild, spec.get("rooms", ())),
        roles=bound_roles(guild, spec.get("roles", ())),
        brain=has_brain(guild) if spec.get("brain") else False,
    )


def module_state(guild, key):
    return modules.status(guild.id, key, **_probe(guild, key))


def module_live(guild, key):
    """Whether a feature should do its work here. The one question every
    entry point asks, and the reason it takes a guild rather than an id:
    a module can be switched on and still be missing the room it posts in,
    and doing half the work is worse than doing none."""
    if guild is None:
        return False
    return modules.live(guild.id, key, **_probe(guild, key))


def module_blockers(guild, key):
    return modules.blockers(guild.id, key, **_probe(guild, key))


def module_summary(guild):
    return modules.summary(
        guild.id, rooms=bound_rooms(guild), roles=bound_roles(guild),
        brain=has_brain(guild),
    )


# ---------- the panel ----------

MARKS = {"on": "🟢", "dormant": "🟡", "blocked": "🟠", "off": "⬜"}


def cooperative_members(guild):
    coop = cooperative_role(guild)
    if coop is None:
        return []
    return [m for m in guild.members if not m.bot and coop in m.roles]


# Whether Apply may adopt a channel that already has the name he would
# have used, or should build his own and leave everything else alone.
#
# It used to adopt unconditionally, which is a decision about somebody
# else's server taken from a string match: a #votes made for something
# quite different quietly became the floor. Build is the default because
# it is the one that cannot be wrong about a room it did not make.
ROOMS_MODE = "rooms_mode"


def adopting(guild) -> bool:
    return settings.get(guild.id, ROOMS_MODE) == "adopt"


def set_adopting(guild, on):
    settings.put(guild.id, **{ROOMS_MODE: "adopt" if on else None})


def adoptable_now(guild):
    """Which jobs there is actually a same-named channel here for, so the
    panel can say what adopting would pick up rather than promising in the
    abstract."""
    found = {}
    for key in modules.wanted_rooms(guild.id):
        if bindings.channel(guild, key) is not None:
            continue
        existing = find_channel(guild, key)
        if existing is not None:
            found[key] = existing
    return found


def panel_content(guild, user, note=None):
    """The whole state of the install on one screen.

    Six lines, in the order somebody would do them, each saying what is
    true rather than what to type. The buttons underneath are the typing.
    """
    bindings.prune(guild)
    have_rooms = bound_rooms(guild)
    need_rooms = modules.required_rooms(guild.id)
    missing = [r for r in need_rooms if r not in have_rooms]
    inside = cooperative_members(guild)
    on, total = modules.counts(guild.id)
    coop, member = cooperative_role(guild), member_role(guild)
    you_in = isinstance(user, discord.Member) and coop is not None and coop in user.roles

    def mark(ok):
        return "✅" if ok else "⬜"

    working = [k for k in modules.keys() if module_state(guild, k) == "on"]
    stuck = [k for k in modules.keys()
             if module_state(guild, k) in ("dormant", "blocked")]

    lines = [
        f"## Eugene in {guild.name}",
        f"**{len(working)}** of **{total}** features are running."
        + (" Press **Apply** to finish the rest." if missing or not inside
           else ""),
        "",
        f"{mark(coop is not None)} **1 · Roles**: "
        + (f"{coop.mention}" + (f" · {member.mention}" if member else "")
           if coop is not None else
           "no cooperative role yet; Apply makes one"),
        f"{mark(not missing)} **2 · Rooms**: "
        + (f"{len(have_rooms)} bound"
           if not missing else
           "missing " + ", ".join(f"`{r}`" for r in missing)
           + (": Apply adopts what is already named that"
              if adopting(guild) else ": Apply makes them")),
        "　　-# Channels: **"
        + ("use the ones I already have" if adopting(guild)
           else "make new ones") + "**"
        + (" · here that would pick up "
           + ", ".join(c.mention for c in adoptable_now(guild).values())
           if adopting(guild) and adoptable_now(guild) else "")
        + ". **Channels** changes it; **Rooms** points a job at any channel "
          "by hand.",
        f"{mark(bool(inside))} **3 · The cooperative**: "
        + (f"{len(inside)} " + ("person" if len(inside) == 1 else "people")
           + ("" if you_in else ", and you are not one of them")
           if inside else
           "**empty: nobody can propose, vote or talk to him.** Apply puts "
           "you in"),
        f"✅ **4 · Features**: {on} on, {total - on} off"
        + (f" · waiting on something: "
           f"{', '.join(modules.name(k).lower() for k in stuck)}"
           if stuck else ""),
        f"{mark(has_brain(guild))} **5 · Brain**: "
        + (f"awake through {providers.label(brain.provider_name(guild.id))}"
           if has_brain(guild) else
           "optional. Governance runs without one, and with no key "
           "**nothing leaves this server**"),
        f"{mark(bool(settings.get(guild.id, 'house')))} **6 · This place**: "
        + (f"*{brain.house_description(guild.id)}*"
           if settings.get(guild.id, "house") else
           "he describes it neutrally rather than guessing"),
    ]
    lacking = builder.missing_permissions(guild)
    if lacking:
        lines.insert(2, "⚠️ **I am missing " + ", ".join(lacking)
                     + ".** Grant them and drag my role to the top of the list.")
    if note:
        lines += ["", note]
    lines.append(
        "-# Nothing in your server changes until you press **Apply**, and "
        "nothing that already exists is ever renamed, moved or deleted."
    )
    return "\n".join(lines)[:1990]


async def show_panel(interaction, note=None, first=False):
    """Draw the panel over whatever is on screen. Every sub-view comes back
    through here, so there is one description of what the panel says."""
    guild = interaction.guild
    content = panel_content(guild, interaction.user, note)
    view = SetupPanel(interaction.user.id)
    if first:
        return await interaction.response.send_message(
            content, view=view, ephemeral=True
        )
    await interaction.response.edit_message(content=content, view=view)


class StewardView(discord.ui.View):
    """Every screen in the panel. Two things every one of them needs: a way
    back, and a check that the person pressing is still allowed to.

    The check is not paranoia about the ephemeral message being seen by
    somebody else -- it cannot be. It is that an administrator can be
    demoted between opening the panel and pressing Apply, and the panel
    should not be the one door in the building that does not notice.
    """

    def __init__(self, owner_id, back=True):
        super().__init__(timeout=600)
        self.owner_id = owner_id
        if back:
            self.add_item(BackButton())

    async def interaction_check(self, interaction):
        if is_steward(interaction):
            return True
        await interaction.response.send_message(STEWARD_ONLY, ephemeral=True)
        return False


class BackButton(discord.ui.Button):
    def __init__(self, row=4):
        super().__init__(label="‹ Back", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction):
        await show_panel(interaction)


class SetupPanel(StewardView):
    def __init__(self, owner_id):
        super().__init__(owner_id, back=False)

    @discord.ui.button(label="Features", style=discord.ButtonStyle.primary, row=0)
    async def features(self, interaction, button):
        await open_modules(interaction)

    @discord.ui.button(label="Rooms", style=discord.ButtonStyle.secondary, row=0)
    async def rooms(self, interaction, button):
        await open_rooms(interaction, 0)

    @discord.ui.button(label="Roles & votes", style=discord.ButtonStyle.secondary, row=0)
    async def roles(self, interaction, button):
        await open_roles(interaction)

    @discord.ui.button(label="Brain", style=discord.ButtonStyle.secondary, row=0)
    async def brain_button(self, interaction, button):
        await open_brain(interaction)

    @discord.ui.button(label="Numbers", style=discord.ButtonStyle.secondary, row=0)
    async def numbers_button(self, interaction, button):
        await open_numbers(interaction)

    @discord.ui.button(label="Channels", style=discord.ButtonStyle.secondary, row=1)
    async def channels(self, interaction, button):
        """The one decision this screen exists to stop him making for you."""
        guild = interaction.guild
        now = adopting(guild)
        set_adopting(guild, not now)
        found = adoptable_now(guild)
        if now:
            note = ("**Channels: make new ones.** Apply builds his own under "
                    "a governance category and binds those. Nothing you "
                    "already have is touched or looked at.")
        elif found:
            note = ("**Channels: use the ones I already have.** Apply will "
                    "adopt " + ", ".join(f"{c.mention} for `{k}`"
                                         for k, c in found.items())
                    + " exactly as they are, and build only what is left. "
                      "Press **Channels** again to go back to building.")
        else:
            note = ("**Channels: use the ones I already have.** Nothing here "
                    "is named like a room he wants, so Apply will build them "
                    "anyway. **Rooms** points a job at a channel whatever it "
                    "is called, which is the way to use one you already have "
                    "under a different name.")
        await show_panel(interaction, note)

    @discord.ui.button(label="Preview the structure", style=discord.ButtonStyle.secondary, row=1)
    async def preview(self, interaction, button):
        guild = interaction.guild
        await interaction.response.edit_message(
            content=structure_preview(guild), view=StewardView(self.owner_id)
        )

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.success, row=1)
    async def apply(self, interaction, button):
        await do_apply(interaction)

    @discord.ui.button(label="What this place is", style=discord.ButtonStyle.secondary, row=1)
    async def house(self, interaction, button):
        await interaction.response.send_modal(HouseModal(interaction.guild))

    @discord.ui.button(label="Invitation message", style=discord.ButtonStyle.secondary, row=2)
    async def invite_message(self, interaction, button):
        await interaction.response.send_modal(InviteDMModal(interaction.guild))

    @discord.ui.button(label="Details", style=discord.ButtonStyle.secondary, row=1)
    async def details(self, interaction, button):
        guild = interaction.guild
        lines = [
            f"## Eugene in {guild.name}",
            module_summary(guild),
            "",
            bindings.summary(
                guild,
                wanted=modules.wanted_rooms(guild.id),
                required=modules.required_rooms(guild.id),
            ),
            "",
            *brain_lines(guild),
            f"Vote window: {numbers(guild)['floor_hours']:g}h (backstop only)"
            # Both tables in the denominator: the overrides above count the
            # switches too, and "12 of 9 numbers set here" is what counting
            # only one of them produces.
            + (f" · {len(settings.voting_overrides(guild.id))} of "
               f"{len(settings.VOTING_RULES) + len(settings.VOTING_FLAGS)} "
               f"set here"
               if settings.voting_overrides(guild.id) else ""),
            f"-# Commit `{COMMIT}`.",
        ]
        await interaction.response.edit_message(
            content="\n".join(lines)[:1990], view=StewardView(self.owner_id)
        )


# ---------- the features ----------

class ModuleSelect(discord.ui.Select):
    """The whole roll in one menu, with what is on already ticked.

    A multi-select hands back the complete selection rather than the one
    thing that changed, which is exactly the shape this wants: what comes
    back *is* the new set of features, and `modules.apply_set` works out
    what moved.
    """

    def __init__(self, guild):
        options = []
        for key in modules.keys():
            entry = modules.spec(key)
            state = module_state(guild, key)
            options.append(discord.SelectOption(
                label=entry["name"],
                value=key,
                description=entry["blurb"][:100],
                emoji=MARKS[state],
                default=modules.switched_on(guild.id, key),
            ))
        super().__init__(
            placeholder="What Eugene does here: tick what you want",
            min_values=0, max_values=len(options), options=options, row=0,
        )

    async def callback(self, interaction):
        guild = interaction.guild
        turned_on, turned_off = modules.apply_set(guild.id, self.values)
        if not turned_on and not turned_off:
            return await open_modules(interaction, "Nothing changed.")
        parts = []
        if turned_on:
            parts.append("**On:** "
                         + ", ".join(modules.name(k) for k in turned_on))
        if turned_off:
            parts.append("**Off:** "
                         + ", ".join(modules.name(k) for k in turned_off))
        log.info(f"guild {guild.id} modules: +{turned_on} -{turned_off}")
        # A feature switched on may want a room that does not exist yet, so
        # say so here rather than leaving it yellow on a screen they have
        # already left.
        wants = [r for r in modules.required_rooms(guild.id)
                 if room(guild, r) is None]
        if wants:
            parts.append("Press **Apply** on the panel to make "
                         + ", ".join(f"`{r}`" for r in wants) + ".")
        await open_modules(interaction, " · ".join(parts))


async def open_modules(interaction, note=None):
    guild = interaction.guild
    view = StewardView(interaction.user.id)
    view.add_item(ModuleSelect(guild))
    body = [
        "## What Eugene does here",
        "Tick a feature to switch it on, untick to switch it off. "
        f"{MARKS['on']} running · {MARKS['dormant']} on, waiting on "
        f"something · {MARKS['blocked']} needs another feature · "
        f"{MARKS['off']} off.",
        "",
        module_summary(guild),
    ]
    if note:
        body += ["", note]
    await interaction.response.edit_message(
        content="\n".join(body)[:1990], view=view
    )


# ---------- rooms ----------
# Bound by id, never by name. Renaming a channel afterwards costs nothing,
# and only the jobs the enabled features actually want are offered: a server
# running him as a moderator is not asked where its votes go.

ROOMS_PER_PAGE = 4


class RoomSelect(discord.ui.ChannelSelect):
    # Announcement channels are text channels that a server decided to
    # publish from, and plenty of servers keep their record in one. They
    # were not on this list, so those servers could not point `decisions`
    # at the channel they already keep decisions in -- the menu simply did
    # not contain it, with nothing to say why.
    KINDS = [discord.ChannelType.text, discord.ChannelType.news]

    def __init__(self, key, row, page=0):
        super().__init__(
            channel_types=self.KINDS,
            placeholder=f"{key}: {bindings.ROOMS[key]}",
            min_values=0, max_values=1, row=row,
        )
        self.key = key
        self.page = page

    async def callback(self, interaction):
        guild = interaction.guild
        if not self.values:
            bindings.bind_channel(guild.id, self.key, None)
            return await open_rooms(interaction, self.page,
                                    f"`{self.key}` unbound.")
        picked = self.values[0]
        bindings.bind_channel(guild.id, self.key, picked.id)
        log.info(f"bound room {self.key} -> #{picked.name} ({picked.id})")
        await open_rooms(
            interaction, self.page,
            f"`{self.key}` → {picked.mention}. Rename it whenever you like; "
            f"I go by id.",
        )


def resolve_channel(guild, text):
    """The channel somebody meant: a mention, an id, a link, or a name.

    Discord's channel menu is auto-populated and renders only part of a long
    list, so a server with a few hundred channels can find that the room it
    wants to point at is simply not in the dropdown, with nothing on screen
    to say why. This is the way round it: no list, say which one you mean.
    """
    text = (text or "").strip()
    if not text:
        return None
    # The last long number in whatever was pasted: a mention is `<#123>`, a
    # link ends in the channel id, and a bare id is itself.
    found = re.findall(r"\d{15,25}", text)
    if found:
        got = guild.get_channel(int(found[-1]))
        if got is not None:
            return got
    wanted = text.lstrip("#").strip().lower()
    pool = list(guild.text_channels)
    exact = next(
        (c for c in pool
         if c.name.lower() == wanted or base_name(c.name) == wanted),
        None,
    )
    return exact or next((c for c in pool if wanted in c.name.lower()), None)


class RoomTypeModal(discord.ui.Modal, title="Point a job at a channel"):
    """The fallback for a channel the menu will not show."""

    job = discord.ui.TextInput(
        label="Which job",
        placeholder="votes",
        style=discord.TextStyle.short,
        max_length=40,
    )
    where = discord.ui.TextInput(
        label="Which channel: name, id, or a link",
        placeholder="#the-floor",
        style=discord.TextStyle.short,
        max_length=200,
    )

    def __init__(self, page=0):
        super().__init__()
        self.page = page

    async def on_submit(self, interaction):
        guild = interaction.guild
        key = str(self.job).strip().strip("`").lower()
        if key not in bindings.ROOMS:
            return await open_rooms(
                interaction, self.page,
                f"There is no job called `{key}`. They are: "
                + ", ".join(f"`{k}`" for k in bindings.ROOMS) + ".",
            )
        picked = resolve_channel(guild, str(self.where))
        if picked is None:
            return await open_rooms(
                interaction, self.page,
                f"I cannot find `{str(self.where).strip()[:60]}` here. A name, "
                "a channel id, or a link to it will all do.",
            )
        bindings.bind_channel(guild.id, key, picked.id)
        log.info(f"bound room {key} -> #{picked.name} ({picked.id}), typed")
        await open_rooms(interaction, self.page,
                         f"`{key}` → {picked.mention}.")


class RoomTypeButton(discord.ui.Button):
    def __init__(self, page):
        super().__init__(label="Type a channel…",
                         style=discord.ButtonStyle.secondary, row=4)
        self.page = page

    async def callback(self, interaction):
        await interaction.response.send_modal(RoomTypeModal(self.page))


class RoomsPageButton(discord.ui.Button):
    def __init__(self, label, page):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=4)
        self.page = page

    async def callback(self, interaction):
        await open_rooms(interaction, self.page)


async def open_rooms(interaction, page=0, note=None):
    guild = interaction.guild
    keys = modules.wanted_rooms(guild.id) or list(bindings.ROOMS)
    pages = [keys[i:i + ROOMS_PER_PAGE]
             for i in range(0, len(keys), ROOMS_PER_PAGE)] or [[]]
    page = max(0, min(page, len(pages) - 1))
    view = StewardView(interaction.user.id)
    for row, key in enumerate(pages[page]):
        view.add_item(RoomSelect(key, row, page))
    view.add_item(RoomTypeButton(page))
    if len(pages) > 1:
        if page:
            view.add_item(RoomsPageButton("‹ Fewer", page - 1))
        if page < len(pages) - 1:
            view.add_item(RoomsPageButton("More ›", page + 1))
    required = modules.required_rooms(guild.id)
    body = [
        "## Which channel does which job?",
        "Pick a channel you already have for each. Clear a menu to unbind "
        "it. Anything left unset that a feature needs, **Apply** creates.",
        "-# Stored by id, so renaming a channel later changes nothing.",
        "-# A menu missing the channel you want is Discord's, not mine: it "
        "lists only part of a long list. Type in it to search, or press "
        "**Type a channel…** and name the one you mean.",
        "",
        bindings.summary(guild, wanted=keys, required=required),
    ]
    if len(pages) > 1:
        body.append(f"-# Page {page + 1} of {len(pages)}.")
    if note:
        body += ["", note]
    await interaction.response.edit_message(
        content="\n".join(body)[:1990], view=view
    )


# ---------- roles, and who holds a vote ----------

class RoleBindSelect(discord.ui.RoleSelect):
    def __init__(self, key, row):
        super().__init__(
            placeholder=f"{key}: {bindings.ROLES[key]}",
            min_values=0, max_values=1, row=row,
        )
        self.key = key

    async def callback(self, interaction):
        guild = interaction.guild
        if not self.values:
            bindings.bind_role(guild.id, self.key, None)
            return await open_roles(interaction, f"`{self.key}` unbound.")
        picked = self.values[0]
        bindings.bind_role(guild.id, self.key, picked.id)
        log.info(f"bound role {self.key} -> {picked.name} ({picked.id})")
        await open_roles(interaction, f"`{self.key}` → {picked.mention}.")


class GrantSelect(discord.ui.UserSelect):
    """The door into the cooperative. Not a shortcut around one: the only
    one there is.

    This used to call itself the bootstrap door and point at `/invite` as
    the ordinary way in, which was two different doors read as one.
    `/invite` is the server's: it ends in a link, and what it lets somebody
    into is the room. Picking up a chore is this, and it is handed over by
    somebody who already has it, because a roll that only a vote can add to
    cannot get its first name.
    """

    def __init__(self, row):
        super().__init__(
            placeholder="Give a vote to…", min_values=0, max_values=10, row=row
        )

    async def callback(self, interaction):
        guild = interaction.guild
        coop = cooperative_role(guild)
        if coop is None:
            return await open_roles(
                interaction,
                "There is no cooperative role here yet. **Apply** makes one.",
            )
        added, refused = [], []
        for member in self.values:
            if member.bot:
                refused.append(f"{member.display_name} is a bot")
                continue
            if coop in member.roles:
                continue
            try:
                await member.add_roles(coop, reason=f"/setup by {interaction.user}")
                added.append(member.display_name)
            except discord.HTTPException as e:
                refused.append(f"{member.display_name}: {e!r}")
        log.info(f"{interaction.user} granted cooperative to {added} in {guild.id}")
        note = (f"In: {', '.join(added)}." if added else "Nobody new.")
        if refused:
            note += " Could not: " + "; ".join(refused) + \
                    ": my role has to sit above the cooperative's."
        await open_roles(
            interaction,
            note + "\n-# This is how the cooperative grows: somebody in it "
                   "hands it over here. `/invite` is a different door: it "
                   "puts a stranger in the server, not on this roll.",
        )


class RevokeSelect(discord.ui.UserSelect):
    """Undoing a typo, not removing a person. An actual removal is
    `/remove`: a vote at the fundamental tier, blind while it runs."""

    def __init__(self, row):
        super().__init__(
            placeholder="Take a vote back (a typo, not a removal)…",
            min_values=0, max_values=10, row=row,
        )

    async def callback(self, interaction):
        guild = interaction.guild
        coop = cooperative_role(guild)
        if coop is None:
            return await open_roles(interaction, "There is no cooperative role here.")
        taken = []
        for member in self.values:
            if coop not in member.roles:
                continue
            try:
                await member.remove_roles(coop, reason=f"/setup by {interaction.user}")
                taken.append(member.display_name)
            except discord.HTTPException as e:
                log.warning(f"could not revoke from {member}: {e!r}")
        log.info(f"{interaction.user} revoked cooperative from {taken} in {guild.id}")
        await open_roles(
            interaction,
            (f"Out: {', '.join(taken)}." if taken else "Nobody changed.")
            + "\n-# For an actual removal, `/remove` puts it to the "
              "cooperative rather than to you.",
        )


class JoinButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Put me in the cooperative",
                         style=discord.ButtonStyle.primary, row=4)

    async def callback(self, interaction):
        guild = interaction.guild
        coop = cooperative_role(guild)
        if coop is None:
            return await open_roles(
                interaction, "There is no cooperative role yet. **Apply** makes one."
            )
        if coop in interaction.user.roles:
            return await open_roles(interaction, "You were already in it.")
        try:
            await interaction.user.add_roles(coop, reason="/setup: the first member")
        except discord.HTTPException as e:
            return await open_roles(
                interaction,
                f"Could not: {e!r}: my role has to sit above {coop.mention}.",
            )
        await open_roles(interaction, f"You are in. {coop.mention} is yours.")


class BellSwitch(discord.ui.Button):
    """The house's answer to whether anybody here is ever pinged.

    Not the same question as whether you personally want telling, which is
    yours and lives on `/house`. This one is a house that has decided its
    rooms are quiet ones, and it outranks the role: switched off, a ballot
    mentions nobody however many people are holding the bell, and nobody
    has to be chased into dropping it one at a time.
    """

    def __init__(self, on):
        super().__init__(
            label="Ballot pings: on" if on else "Ballot pings: off",
            style=discord.ButtonStyle.secondary, row=4,
        )

    async def callback(self, interaction):
        guild = interaction.guild
        now = ringing(guild)
        set_ringing(guild, not now)
        log.info(f"guild {guild.id}: ballot pings -> {'off' if now else 'on'}")
        if now:
            return await open_roles(
                interaction,
                "**Ballot pings off.** No proposal mentions anybody here "
                "again. Whoever holds the bell keeps it and stops being "
                "rung.",
            )
        await open_roles(
            interaction,
            "**Ballot pings on.** A new ballot mentions whoever asked to be "
            "told, on the ballot's own card. Nobody is asking yet unless "
            "they have picked the role up under `/house`.",
        )


async def open_roles(interaction, note=None):
    guild = interaction.guild
    view = StewardView(interaction.user.id)
    view.add_item(RoleBindSelect("cooperative", 0))
    view.add_item(RoleBindSelect("member", 1))
    view.add_item(GrantSelect(2))
    view.add_item(RevokeSelect(3))
    view.add_item(JoinButton())
    view.add_item(BellSwitch(ringing(guild)))
    inside = cooperative_members(guild)
    body = [
        "## Who holds a vote",
        "`cooperative` votes. `member` is in the room without one: leave it "
        "unbound if everyone here votes. `bell` is who a new ballot pings, "
        "and it is picked up under `/house` by whoever wants it rather than "
        "handed out from here.",
        "",
        f"**In the cooperative: {len(inside)}**"
        + (": " + ", ".join(m.display_name for m in inside[:20])
           + ("…" if len(inside) > 20 else "")
           if inside else
           ": **nobody. He refuses everyone, including you, until somebody "
           "is in.**"),
        "",
        bindings.summary(guild, wanted=[], required=[]),
    ]
    if note:
        body += ["", note]
    await interaction.response.edit_message(
        content="\n".join(body)[:1990], view=view
    )


# ---------- the brain ----------

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
        label="Model for talking (blank for the default)",
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
        woken = [modules.name(k) for k in ("chat", "memory")
                 if modules.enabled(self.guild.id, k)]
        await interaction.followup.send(
            f"Done. He is awake through {providers.label(self.annex)} on "
            f"`{model}`, and the key ({settings.fingerprint(key)}) stays here.\n"
            + (f"-# That wakes {', '.join(woken).lower()}.\n" if woken else "")
            + (
                f"-# {' and '.join(providers.label(n) for n in others)} "
                f"still on file; the Brain screen switches between them.\n"
                if others else ""
            )
            + "-# Mention him to talk.",
            ephemeral=True,
        )


# Whoever accepted what a key means, and when. A server that has never
# been told is asked once, before the first key, and never again.
CONSENT = "ai_consent"


def consented(guild) -> bool:
    return bool(settings.get(guild.id, CONSENT))


CONSENT_NOTICE = (
    "## Before a key goes in\n"
    "Setting one changes what this server sends outside it, so somebody "
    "should read this rather than find out later.\n\n"
    "**What goes out, and only when a member of the cooperative speaks to "
    "him in a room he may answer in:**\n"
    "- up to 40 recent messages from that room, with display names\n"
    "- the tool results from that room, the roster count, and which "
    "features are on\n\n"
    "**Where:** the provider whose key you are about to paste, and no "
    "other. Their retention and training terms are theirs, and they "
    "change: read them.\n\n"
    "**What never goes out:** how anybody voted, anything about people "
    "who are not in the room, and any durable note about a person -- he "
    "keeps none. Nothing is sent on a timer.\n\n"
    "**He does not read rooms he cannot answer in.** Bind a chat room and "
    "that is the only room he sees.\n\n"
    "-# The full version, with the line of code that does each part, is "
    "PRIVACY.md. `/privacy` shows any member the same thing for this "
    "server at any time. Everything Eugene exists to do works with no key "
    "at all."
)


class ConsentView(StewardView):
    """One screen, once. Accepting is recorded under a name, because "the
    server agreed" is not a thing a server can do -- a person does it."""

    def __init__(self, owner_id, annex):
        super().__init__(owner_id)
        self.annex = annex

    @discord.ui.button(label="I have read this: set the key",
                       style=discord.ButtonStyle.primary, row=0)
    async def accept(self, interaction, button):
        settings.put(
            interaction.guild.id,
            **{CONSENT: {"by": interaction.user.display_name,
                         "id": interaction.user.id,
                         "at": now_utc().isoformat()}},
        )
        log.info(f"guild {interaction.guild.id}: AI terms accepted by "
                 f"{interaction.user.display_name}")
        await interaction.response.send_modal(
            BrainKeyModal(interaction.guild, self.annex)
        )


class SetKeyButton(discord.ui.Button):
    def __init__(self, annex, keyed):
        super().__init__(
            label=("Replace " if keyed else "Set ") + providers.label(annex),
            style=discord.ButtonStyle.primary if not keyed
            else discord.ButtonStyle.secondary,
            row=2,
        )
        self.annex = annex

    async def callback(self, interaction):
        # The notice comes before the modal, not after: a key pasted and
        # then explained is a key already pasted.
        if not consented(interaction.guild):
            return await interaction.response.edit_message(
                content=CONSENT_NOTICE,
                view=ConsentView(interaction.user.id, self.annex),
            )
        await interaction.response.send_modal(
            BrainKeyModal(interaction.guild, self.annex)
        )


class OnDutySelect(discord.ui.Select):
    def __init__(self, guild, keyed):
        on_duty = brain.provider_name(guild.id)
        super().__init__(
            placeholder="Which one does the talking",
            min_values=1, max_values=1, row=0,
            options=[
                discord.SelectOption(
                    label=providers.label(n),
                    value=n,
                    description=f"on `{brain.model_name(guild.id, n)}`"[:100],
                    default=(n == on_duty),
                ) for n in keyed
            ],
        )

    async def callback(self, interaction):
        guild = interaction.guild
        picked = self.values[0]
        settings.set_provider(guild.id, picked)
        brain.forget_client(guild.id)
        log.info(f"guild {guild.id} switched to {picked}")
        await open_brain(
            interaction,
            f"{providers.label(picked)} it is, on `{brain.model_name(guild.id)}`. "
            f"Any other key stays on file.",
        )


class ForgetKeySelect(discord.ui.Select):
    def __init__(self, guild, keyed):
        super().__init__(
            placeholder="Forget a key…", min_values=0, max_values=1, row=1,
            options=[
                discord.SelectOption(
                    label=providers.label(n),
                    value=n,
                    description=f"key {settings.fingerprint(settings.brain_key(guild.id, n))}",
                ) for n in keyed
            ],
        )

    async def callback(self, interaction):
        guild = interaction.guild
        if not self.values:
            return await open_brain(interaction)
        annex = self.values[0]
        settings.clear_brain_key(guild.id, annex)
        brain.forget_client(guild.id)
        log.info(f"{annex} key cleared for guild {guild.id}")
        still = brain.provider_name(guild.id)
        await open_brain(
            interaction,
            "Forgotten. " + (
                f"He carries on through {providers.label(still)}."
                if still else
                "He keeps the records and holds the door as before, and says "
                "nothing."
            ),
        )


async def open_brain(interaction, note=None):
    guild = interaction.guild
    keyed = settings.keyed_providers(guild.id, providers.NAMES)
    view = StewardView(interaction.user.id)
    if len(keyed) > 1:
        view.add_item(OnDutySelect(guild, keyed))
    if keyed:
        view.add_item(ForgetKeySelect(guild, keyed))
    for annex in providers.NAMES:
        view.add_item(SetKeyButton(annex, annex in keyed))
    body = [
        "## Giving him something to think with",
        "He keeps records and holds the door with no AI at all. A key is "
        "what makes him talk, and what wakes conversation, memory and the "
        "heartbeat.",
        "-# Gemini is Google AI Studio · Grok is console.x.ai · Claude is "
        "console.anthropic.com. Each server pays for its own thinking.",
        "",
        *brain_lines(guild),
    ]
    if note:
        body += ["", note]
    await interaction.response.edit_message(
        content="\n".join(body)[:1990], view=view
    )


# ---------- starting again ----------
# ---------- the numbers this house votes by ----------
# The shape of a vote is code and stays code. The numbers are the house's,
# and the reason they are here rather than in the repo is that they were
# already meant to be: the parliament is supposed to legislate its own
# thresholds, and until now passing a decision that changed one still
# needed somebody to push a commit before it was true.
#
# This is the steward's door, not the cooperative's. That is not a claim
# that thresholds are an administrator's business -- they are exactly not
# -- it is that a proposal has no hands. When the executor lands, a passed
# decision comes through this same function and the steward stops being the
# only way in.

# What each number means lives in settings.py, beside its bounds. How one
# reads on a screen, and how far back changing it goes, live in powers.py,
# where the tool that changes one by conversation can reach them too: this
# panel is no longer the only door onto these.
VOTING_BLURBS = settings.VOTING_HELP
VOTING_SWITCHES = settings.VOTING_FLAG_HELP
shown_value = powers.shown


def voting_lines(guild):
    """Every number and switch, what it means, and whether it is theirs or
    his."""
    held = numbers(guild)
    chosen = settings.voting_overrides(guild.id)
    rows = []
    for table in (VOTING_BLURBS, VOTING_SWITCHES):
        for name, blurb in table.items():
            # Not `-#` subtext: Discord only honours that at the start of a
            # line, and this is the end of one.
            mark = "" if name in chosen else " *(his default)*"
            rows.append(
                f"- `{name}` **{shown_value(name, held[name])}**: {blurb}{mark}"
            )
    return rows


class NumberModal(discord.ui.Modal):
    """One number at a time on purpose. These decide what carries and what
    does not, and a form that changes six of them at once is a form
    somebody will change six of them at once with."""

    value = discord.ui.TextInput(
        label="New value", style=discord.TextStyle.short, max_length=20
    )

    def __init__(self, guild, number):
        super().__init__(title=number[:45])
        self.guild = guild
        self.number = number
        low, high = settings.VOTING_RULES[number][1:3]
        self.value.placeholder = (
            f"between {low:g} and {high:g}, or `default`"
        )
        self.value.default = f"{numbers(guild)[number]:g}"

    async def on_submit(self, interaction):
        guild, number = self.guild, self.number
        raw = str(self.value).strip()
        was = numbers(guild)[number]
        wanted = None if raw.lower() in ("default", "reset", "") else raw
        accepted, rejected = settings.set_voting(guild.id, **{number: wanted})
        if rejected:
            low, high = settings.VOTING_RULES[number][1:3]
            return await open_numbers(
                interaction,
                f"`{raw}` is not a number `{number}` can be. It lives between "
                f"{low:g} and {high:g}.",
            )
        now = accepted[number]
        # Say so when a value was pulled inside its bounds. Comparing the
        # cast of what they typed against what was stored, never the
        # strings: "6" and 6.0 are the same number and reporting them as a
        # correction would teach people to distrust the message that matters.
        asked = None
        if wanted is not None:
            try:
                asked = settings.VOTING_RULES[number][3](wanted)
            except (TypeError, ValueError):
                asked = None
        held = " (held at the nearest it can be)" if asked is not None and asked != now else ""
        tail = powers.reaches(number)
        await health_log(
            guild,
            f"⚙️ `{number}` {was:g} → {now:g}, by {interaction.user.display_name}.",
        )
        log.info(f"voting number {number}: {was} -> {now} "
                 f"by {interaction.user.display_name}")
        await open_numbers(
            interaction,
            f"`{number}` was **{was:g}**, and is now **{now:g}**{held}. {tail}",
        )


class NumberSelect(discord.ui.Select):
    def __init__(self, guild):
        super().__init__(
            placeholder="Change one…", min_values=0, max_values=1, row=0,
            options=[
                discord.SelectOption(
                    label=name,
                    value=name,
                    description=blurb[:100],
                ) for name, blurb in VOTING_BLURBS.items()
            ],
        )

    async def callback(self, interaction):
        if not self.values:
            return await open_numbers(interaction)
        await interaction.response.send_modal(
            NumberModal(interaction.guild, self.values[0])
        )


class SwitchSelect(discord.ui.Select):
    """The on/off half. No modal behind it: there are two states and asking
    somebody to type one of them is a form for nothing."""

    def __init__(self, guild):
        held = numbers(guild)
        super().__init__(
            placeholder="Turn one on or off…", min_values=0, max_values=1, row=1,
            options=[
                discord.SelectOption(
                    label=f"{name}: {shown_value(name, held[name])}",
                    value=name,
                    description=blurb[:100],
                ) for name, blurb in VOTING_SWITCHES.items()
            ],
        )

    async def callback(self, interaction):
        if not self.values:
            return await open_numbers(interaction)
        guild, name = interaction.guild, self.values[0]
        was = numbers(guild)[name]
        settings.set_voting(guild.id, **{name: not was})
        now = numbers(guild)[name]
        await health_log(
            guild,
            f"⚙️ `{name}` {shown_value(name, was)} → {shown_value(name, now)}, "
            f"by {interaction.user.display_name}.",
        )
        log.info(f"voting switch {name}: {was} -> {now} "
                 f"by {interaction.user.display_name}")
        await open_numbers(
            interaction,
            f"`{name}` is **{shown_value(name, now)}**. "
            + powers.reaches(name),
        )


async def open_numbers(interaction, note=None):
    guild = interaction.guild
    view = StewardView(interaction.user.id)
    view.add_item(NumberSelect(guild))
    view.add_item(SwitchSelect(guild))
    body = [
        f"## What {guild.name} votes by",
        *voting_lines(guild),
        "-# Each is held inside a range where the rest of the machinery "
        "still means what it says.",
    ]
    if note:
        body += ["", note]
    if interaction.response.is_done():
        return await interaction.edit_original_response(
            content="\n".join(body)[:1990], view=view
        )
    await interaction.response.edit_message(
        content="\n".join(body)[:1990], view=view
    )


# ---------- what this place is ----------

class HouseModal(discord.ui.Modal, title="What is this server for?"):
    """The server's own words about itself, which Eugene is told.

    He used to be told, in as many words, that he ran a particular house
    full of friends who wanted to play games without a headache. That was
    true of one server and false everywhere else, and it is the sort of
    falsehood he repeats confidently. The server's name he reads off
    Discord; what the place is for, only its people know.

    Stored per guild and constant between edits, so it sits in the cached
    half of the prompt and costs nothing to send on every request.
    """

    description = discord.ui.TextInput(
        label="One line. Blank resets it.",
        style=discord.TextStyle.short,
        required=False,
        max_length=300,
        placeholder="a book club that argues about endings",
    )
    # How this house wants him to sound, in its own words.
    #
    # Character is the one thing that genuinely is a server's business and
    # not the repo's: a co-op, a study group and a guild of friends want
    # three different clerks, and the shipped default cannot be all of
    # them. It goes in the cached half like the description, because it
    # changes when somebody edits it and not otherwise.
    #
    # It never reaches the rules. Whatever is written here, the ballot
    # arithmetic, the sealed votes and the refusals are code, and the hard
    # rules sit below this in the prompt where nothing above can argue
    # with them.
    voice = discord.ui.TextInput(
        label="How should he sound? Blank for the default.",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1200,
        placeholder="Warm, informal, calls people comrade. Has opinions "
                    "and says them when asked.",
    )

    def __init__(self, guild):
        super().__init__()
        current = settings.get(guild.id, "house")
        if current:
            self.description.default = current
        voiced = settings.get(guild.id, "voice")
        if voiced:
            self.voice.default = voiced

    async def on_submit(self, interaction):
        text = " ".join(str(self.description).split())[:300]
        tone = str(self.voice).strip()[:1200]
        settings.put(interaction.guild.id, house=text or None,
                     voice=tone or None)
        note = (
            f"He now knows **{interaction.guild.name}** as *{text}*."
            if text else
            f"Reset. He will describe this place as "
            f"*{brain.DEFAULT_HOUSE}*: true of most servers, specific to none."
        )
        await show_panel(interaction, note)


# ---------- the word after an invitation carries ----------

def invite_dm_preview(guild, user):
    """What the house's template comes out as, with a stand-in for
    everything only a real invitation knows. Shown at the moment somebody
    writes one, because a template with a typo in a placeholder reads
    perfectly well until it is filled in."""
    return invite_dm(
        guild,
        {"no": 12, "invitee": "Sam"},
        user,
        "https://discord.gg/example",
    )


class InviteDMModal(discord.ui.Modal, title="After an invitation passes"):
    """The private message the proposer gets with the link in it.

    A house writes its own or takes his. Nothing about the door changes
    here: the vote, the veto window and the single-use link are code, and
    this is only the sentence they arrive wrapped in.
    """

    # Written off the same table `invite_dm` fills from, so the list
    # somebody is shown cannot come to differ from the list that works.
    message = discord.ui.TextInput(
        label="Blank for his own words.",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
        placeholder=" ".join(f"{{{f}}}" for f in INVITE_DM_FIELDS)[:100],
    )

    def __init__(self, guild):
        super().__init__()
        # Prefilled with whatever is in force, his default included, so
        # somebody who wants a small change to the shipped sentence edits
        # it rather than retyping it from the screen behind this one.
        self.message.default = (
            settings.get(guild.id, INVITE_DM) or DEFAULT_INVITE_DM
        )

    async def on_submit(self, interaction):
        guild = interaction.guild
        text = str(self.message).strip()[:1000]
        # Submitting the prefill unchanged is not a house writing his
        # sentence down as its own: it is a house that did not want to
        # change it, and it should keep following the default rather than
        # holding a frozen copy of what the default was today.
        own = text if text and text != DEFAULT_INVITE_DM else None
        settings.put(guild.id, **{INVITE_DM: own})
        log.info(f"guild {guild.id} invite DM: "
                 f"{'set' if own else 'back to the default'} "
                 f"by {interaction.user.display_name}")
        await health_log(
            guild,
            "⚙️ The message after an invitation carries is "
            + ("the house's own" if own else "back to his default")
            + f", by {interaction.user.display_name}.",
        )
        preview = "\n".join(
            f"> {line}" for line in
            invite_dm_preview(guild, interaction.user).splitlines()
        )
        await show_panel(
            interaction,
            ("**The word after an invitation passes** is yours now."
             if own else
             "**The word after an invitation passes** is his own again.")
            + " It goes privately to whoever proposed them:\n"
            + preview
            + "\n-# The link is added on the end if the message leaves "
              "`{link}` out, so nobody is congratulated and given nothing.",
        )


# ---------- the structure that comes out ----------

def structure_preview(guild):
    """What this server will look like, before it looks like it.

    Generated from the features that are switched on, so it cannot describe
    a layout other than the one Apply actually produces. Three marks: a
    room that exists and is bound, a room that exists under the name he
    would have used and will be adopted exactly as it stands, and a room
    he would create.
    """
    lines = [f"## {guild.name}, once you press Apply"]
    coop, member = cooperative_role(guild), member_role(guild)
    lines += [
        "**Roles**",
        f"{'✅' if coop else '➕'} `Cooperative`: holds a vote"
        + (f" (you have {coop.mention})" if coop else ""),
        f"{'✅' if member else '➕'} `Member`: in the room, no vote",
        "",
    ]
    plan = modules.structure(guild.id, only_buildable=True)
    if not plan:
        lines.append("*No feature that needs a room is switched on, so "
                     "there is nothing to build.*")
    for category, rooms in plan:
        existing_cat = category_for(guild, category)
        lines.append(
            f"{'✅' if existing_cat else '➕'} **{category or 'no category'}**"
            + (f": {modules.CATEGORIES[category]}"
               if category in modules.CATEGORIES else "")
        )
        for job in rooms:
            spec = modules.ROOM_PLAN[job]
            bound = bindings.channel(guild, job)
            found = None if bound else find_channel(guild, job)
            if bound:
                mark, shown, tail = "✅", bound.mention, "already bound"
            elif found and adopting(guild):
                mark, shown, tail = "🔗", found.mention, "adopted as it is"
            else:
                mark, shown, tail = "➕", f"#{spec['name']}", "created"
            wanted = modules.wanted_by(guild.id, job)
            lines.append(
                f"　{mark} {shown}: {tail}; "
                + {"cooperative": "the cooperative's",
                   "members": "everyone in the room",
                   "admins": "administrators only",
                   "gate": "everyone, role or no role"}[spec["visibility"]]
                + f" · for {', '.join(modules.name(k).lower() for k in wanted)}"
            )
        lines.append("")
    off = [modules.name(k) for k in modules.keys()
           if not modules.enabled(guild.id, k)]
    if off:
        lines.append("Switched off, so nothing is built for them: "
                     + ", ".join(off) + ".")
    lines.append(
        "-# Every line above is additive. "
        + ("A channel that already exists is adopted exactly as it stands: "
           "never renamed, re-topiced, moved, re-permissioned or deleted."
           if adopting(guild) else
           "He is building his own; nothing you already have is touched or "
           "even looked at. **Channels** on the panel switches that.")
        + " Nothing else in your server is touched."
    )
    return "\n".join(lines)[:1990]


def category_for(guild, name):
    """The category a job's rooms belong in: the one bound to that key, else
    one already called that, else none yet."""
    if not name:
        return None
    return (bindings.category(guild, name)
            or discord.utils.get(guild.categories, name=name))


async def ensure_categories(guild, wanted, say):
    """Create or adopt the categories the plan needs, and bind them.

    Adopting by name rather than making a second one is the same meekness
    `bindings.adopt` has: a server that already has a category called
    governance meant that one.
    """
    made = {}
    for name in wanted:
        existing = category_for(guild, name)
        if existing is None:
            try:
                existing = await guild.create_category(
                    name, reason="Created by /setup"
                )
                say(f"created category **{name}**")
            except (discord.HTTPException, AttributeError) as e:
                log.warning(f"could not make category {name}: {e!r}")
                continue
        bindings.bind_category(guild.id, name, existing.id)
        made[name] = existing
    return made


async def make_missing_rooms(guild, categories=None, adopt=False):
    """Create and bind a channel for every job an enabled feature wants and
    has none. Strictly additive: it never renames, moves, re-topics,
    re-permissions, reorders or deletes anything that already exists.
    Nothing outside the plan is touched at all.

    `adopt` decides what happens when a channel already has the name he
    would have used. It used to be unconditional and it was a guess the
    server never agreed to: a room called #votes, made for something else,
    silently became the floor. The choice is the house's now -- Channels on
    the panel is where it is made -- and the default is to build his own
    and leave everything else alone.

    Which rooms those are comes from `modules.py`, so a server that has
    switched governance off is not given a votes room, and switching a
    feature on and pressing Apply is the whole of adding one later.

    Returns (made, bound, skipped) as lists of display lines.
    """
    coop = cooperative_role(guild)
    categories = categories or {}
    made, bound, skipped = [], [], []
    for key in modules.wanted_rooms(guild.id, only_buildable=True):
        spec = modules.ROOM_PLAN[key]
        if bindings.channel(guild, key) is not None:
            skipped.append(f"`{key}` already bound")
            continue
        # Only if the house said so. `find_channel` is the same lookup the
        # rest of the bot uses for a room nobody has bound, which is the
        # point: what gets bound here and what gets found without a binding
        # must be the same channel.
        existing = find_channel(guild, key) if adopt else None
        if existing is not None:
            bindings.bind_channel(guild.id, key, existing.id)
            bound.append(f"`{key}` → {existing.mention} (adopted, unchanged)")
            continue
        # An empty dict, never None: discord.py reads a mapping as "these
        # exact overwrites" and MISSING as "none", but None is neither and
        # it raises rather than defaulting. `{}` is the one that means what
        # is meant here -- inherit the category, touch nothing. This crashed
        # `/setup` on the first room that is open to everybody, which is to
        # say on every fresh install.
        overwrites = {}
        if spec["visibility"] == "cooperative" and coop is not None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                coop: discord.PermissionOverwrite(view_channel=True),
            }
        elif spec["visibility"] == "admins":
            overwrites = admin_only_overwrites(guild)
        extra = {}
        parent = categories.get(spec["category"])
        if parent is not None:
            extra["category"] = parent
        try:
            made_channel = await guild.create_text_channel(
                spec["name"], topic=spec["topic"], overwrites=overwrites,
                reason="Created by /setup", **extra,
            )
        except discord.HTTPException as e:
            skipped.append(f"`{key}` could not be created: {e!r}")
            continue
        bindings.bind_channel(guild.id, key, made_channel.id)
        made.append(f"`{key}` → {made_channel.mention}")

    # Rooms he will use but never make. A server that already has somewhere
    # people say hello gets that room bound and nothing built; a server that
    # does not is left alone, and the feature reads as dormant until
    # somebody points it somewhere. This is the difference between fitting
    # into a server and rearranging one.
    for key in modules.adoptable_rooms(guild.id):
        if bindings.channel(guild, key) is not None:
            continue
        existing = find_channel(guild, key) if adopt else None
        if existing is None:
            skipped.append(f"`{key}`: nothing here to use, so nothing made")
            continue
        bindings.bind_channel(guild.id, key, existing.id)
        bound.append(f"`{key}` → {existing.mention} (adopted, unchanged)")
    return made, bound, skipped


async def do_apply(interaction):
    """Build what the switches add up to, and leave somebody inside.

    Everything here was already possible -- create a role by hand, bind it,
    give it to yourself, then create the rooms -- but the first step of that
    chain happened outside Discord's slash commands, so a new server
    arrived with nobody in the cooperative and no way in. Eugene then
    refused every single person, including the one who installed him, and
    named no route out of it. That is the bug this fixes: one button that
    leaves somebody inside.

    Additive throughout. It adopts an existing Cooperative role rather than
    making a second one, and never takes a role away from anybody.
    """
    guild = interaction.guild
    lacking = builder.missing_permissions(guild)
    if lacking:
        return await interaction.response.edit_message(
            content="I cannot do this yet: I am missing: "
            + ", ".join(lacking)
            + ".\nGrant them, drag my role to the top of the list, and press "
              "Apply again.",
            view=SetupPanel(interaction.user.id),
        )
    await interaction.response.defer()

    steps = []

    def say(line):
        steps.append(f"- {line}")

    skipped = []
    try:
        # Roles first: the rooms below want the cooperative role to hide behind.
        coop = await builder.ensure_cooperative(guild, say)
        member = await builder.ensure_member(guild, say)
        bindings.bind_role(guild.id, "cooperative", coop.id)
        bindings.bind_role(guild.id, "member", member.id)
        say(f"bound `cooperative` → {coop.mention} and "
            f"`member` → {member.mention}")
        # The rest of what the switched-on features want. Made empty and
        # bound: a role nobody holds pings nobody, so this leaves the door
        # open without putting anybody through it.
        for key, made in (await builder.ensure_module_roles(guild, say)).items():
            bindings.bind_role(guild.id, key, made.id)
            say(f"bound `{key}` → {made.mention}")

        # The whole point: somebody ends up inside.
        you = interaction.user
        if coop in you.roles:
            say("you were already in the cooperative")
        else:
            try:
                await you.add_roles(coop, reason="/setup: the first member")
                say(f"gave you {coop.mention}")
            except discord.HTTPException as e:
                say(f"could not give you {coop.mention}: {e!r}: "
                    "check my role sits above it in the list")

        wanted = [c for c, _rooms
                  in modules.structure(guild.id, only_buildable=True)]
        categories = await ensure_categories(guild, wanted, say)
        made, bound, skipped = await make_missing_rooms(
            guild, categories, adopt=adopting(guild))
        for line in made:
            say(f"created {line}")
        for line in bound:
            say(f"found and bound {line}")
    except Exception as e:
        # Half a build is a state somebody has to be told about: Apply is
        # idempotent, so the answer is almost always "press it again", but
        # only if they know it stopped.
        log.error(f"/setup apply failed in {guild.id}: {e!r}")
        return await interaction.edit_original_response(
            content="\n".join([
                "## Apply stopped partway",
                f"`{e!r}`",
                "",
                "What it managed first:",
                *steps,
                "",
                "-# Nothing was renamed, moved or deleted. Apply is safe to "
                "press again: it picks up where it stopped.",
            ])[:1990],
            view=StewardView(interaction.user.id),
        )

    lines = [
        "## Applied",
        "Nothing that already existed was renamed, moved or re-permissioned.",
        "",
        *steps,
    ]
    left = []
    if not has_brain(guild):
        left.append("**Brain**: a key, so he can talk. Without one he keeps "
                    "records and holds the door, and says nothing.")
    left.append(
        "**Roles & votes**: hand the cooperative to anyone else who should "
        "have one. That is the only way onto that roll; `/invite` is the "
        "server's door and a vote, and it hands out a link, not a ballot."
    )
    if not settings.get(guild.id, "house"):
        left.append(
            "**What this place is**: one line on what this server is for. He "
            "reads the name off Discord, but not the point of the place."
        )
    stuck = [k for k in modules.keys()
             if module_state(guild, k) in ("dormant", "blocked")]
    if stuck:
        left.append("Still waiting on something: "
                    + ", ".join(modules.name(k) for k in stuck)
                    + ". **Details** says what.")
    if skipped:
        left.append("Left alone: " + ", ".join(skipped) + ".")
    lines += ["", "**What is left:**"] + [f"- {item}" for item in left]
    await interaction.edit_original_response(
        content="\n".join(lines)[:1990],
        view=StewardView(interaction.user.id),
    )


@bot.tree.command(
    name="setup", description="Install and configure Eugene in this server"
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def slash_setup(interaction: discord.Interaction):
    if not is_steward(interaction):
        return await interaction.response.send_message(
            STEWARD_ONLY, ephemeral=True
        )
    await show_panel(interaction, first=True)


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


def module_note(guild, key):
    """Why a feature is not answering, in a sentence somebody can act on.
    None when it is working, which is the ordinary case.

    Every command and every event that belongs to a feature asks this
    first. Saying "that is switched off here" is the whole point of having
    modules at all: the alternative is a command that quietly does nothing,
    which is indistinguishable from a broken bot.
    """
    state = module_state(guild, key)
    if state == "on":
        return None
    label = modules.name(key)
    if state == "off":
        return (f"{label} is switched off in this server. Whoever runs the "
                f"place can switch it back on in `/setup`.")
    why = module_blockers(guild, key)
    return (f"{label} is switched on but not running yet: "
            + "; ".join(why) + ". `/setup` is where that is fixed.")


async def refuse_unless(interaction, key):
    """Refuse and return True when this feature is not running here."""
    note = module_note(interaction.guild, key)
    if note is None:
        return False
    await refuse(interaction, note)
    return True


@bot.tree.command(name="propose", description="Say what should change, and why")
@app_commands.describe(
    priority="Chase the cooperative about it by DM. For the ones that matter."
)
@app_commands.guild_only()
async def slash_propose(interaction: discord.Interaction, priority: bool = False):
    if await refuse_unless(interaction, "governance"):
        return
    if not keyed_in(interaction):
        return await refuse(interaction, NOT_INSIDE)
    await interaction.response.send_modal(BillModal(priority=priority))


@bot.tree.command(
    name="invite", description="Propose that someone be invited to the server"
)
@app_commands.guild_only()
async def slash_invite(interaction: discord.Interaction):
    if await refuse_unless(interaction, "governance"):
        return
    if not keyed_in(interaction):
        return await refuse(interaction, "That one is the cooperative's.")
    await interaction.response.send_modal(InviteModal())


@bot.tree.command(name="remove", description="Propose that someone be removed")
@app_commands.guild_only()
async def slash_remove(interaction: discord.Interaction):
    if await refuse_unless(interaction, "governance"):
        return
    if not keyed_in(interaction):
        return await refuse(interaction, "That one is the cooperative's.")
    # The weight of the thing is said before the picker, not after, so
    # nobody names a person without having read what it costs.
    await interaction.response.send_message(
        removal_weight(interaction.guild),
        view=KickTargetView(),
        ephemeral=True,
    )


@bot.tree.command(name="close", description="Call time on a vote that has had its run")
@app_commands.describe(number="Which proposal")
@app_commands.guild_only()
async def slash_close(interaction: discord.Interaction, number: int):
    if await refuse_unless(interaction, "governance"):
        return
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
    if report.get("tally"):
        lines.append(report["tally"])
    if report.get("outstanding"):
        lines += ["", "Still wanted:"] + [f"- {item}" for item in report["outstanding"]]
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(
    name="poll", description="Ask the whole server a question. It decides nothing"
)
@app_commands.guild_only()
async def slash_poll(interaction: discord.Interaction):
    if await refuse_unless(interaction, "polls"):
        return
    # Opening one is the cooperative's, answering one is everybody's, and
    # that split is the feature rather than an oversight. A room where
    # anyone can put a question to everyone is a room with a question in it
    # every day; the people who carry the place decide what it is asked.
    if not keyed_in(interaction):
        return await refuse(
            interaction,
            "A community poll is put up by the cooperative, though anyone "
            "here can answer one. " + NOT_INSIDE,
        )
    note = poll_room_note(interaction.guild)
    if note:
        return await refuse(interaction, note)
    await interaction.response.send_modal(PollModal())


@bot.tree.command(name="bills", description="What is open for a vote right now")
@app_commands.guild_only()
async def slash_bills(interaction: discord.Interaction):
    """What is open, to the cooperative it belongs to."""
    if await refuse_unless(interaction, "governance"):
        return
    if not keyed_in(interaction):
        return await refuse(interaction, NOT_INSIDE)

    filed = load_json(bills_path(interaction.guild), [])
    open_bills = sorted(
        (b for b in filed if b.get("status") == "on_floor"),
        key=lambda b: b["no"],
    )
    # A proposal in its window is closed and not finished, which is exactly
    # the thing this command is for: somewhere to look and see what still
    # wants you. Leaving it out is how somebody misses a window.
    windows = sorted((b for b in filed if veto_open(b)), key=lambda b: b["no"])
    if not open_bills and not windows:
        return await interaction.response.send_message(
            "Nothing is open. The floor is yours.", ephemeral=True
        )
    lines = []
    where = floor_for(interaction.guild, {})
    if open_bills:
        lines.append("**Open for a vote**")
        # A title can run to a hundred characters, so a busy floor is
        # trimmed rather than sent and refused by Discord for length.
        for bill in open_bills[:10]:
            ends = int(datetime.fromisoformat(bill["ends_at"]).timestamp())
            mark = "⚡ " if is_priority(bill) else ""
            lines.append(
                f"{mark}**No. {bill['no']}: {bill['title']}**: "
                f"{bill.get('author', 'someone')}, closes <t:{ends}:R>"
                + (f" · {where.mention}" if where else "")
            )
        rest = len(open_bills) - 10
        if rest > 0:
            lines.append(f"-# And {rest} more.")
        lines.append("-# ⚡ is one you will be chased about.")
    if windows:
        # The window is on the decision now, not on the floor, so this
        # points at the record: sending somebody to the wrong room to find
        # a button that shuts in an hour is how a window gets missed.
        record = record_for(interaction.guild, {})
        lines.append("")
        lines.append("**Passed, and can still be taken back**")
        for bill in windows[:5]:
            shuts = int(datetime.fromisoformat(bill["veto"]["until"]).timestamp())
            lines.append(
                f"🛑 **No. {bill['no']}: {bill['title']}**: "
                f"the window shuts <t:{shuts}:R>"
                + (f" · {record.mention}" if record else "")
            )
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------- what he can see, and what leaves ----------
# Both of these are the cooperative's, not the steward's. A privacy notice
# only one person can read is a notice, not a disclosure -- and the whole
# claim being made here is that anybody can check it rather than take his
# word for it. So both are computed live from Discord and from the code,
# and neither quotes PRIVACY.md, which would only be a page agreeing with
# itself.


def _visible_rooms(guild):
    """Every text channel he can actually read, by Discord's own answer."""
    me = guild.me
    out = []
    for channel in guild.text_channels:
        try:
            if channel.permissions_for(me).read_messages:
                out.append(channel)
        except Exception:
            continue
    return out


@bot.tree.command(name="access",
                  description="Which channels Eugene can see, listen in and answer in")
@app_commands.guild_only()
async def slash_access(interaction: discord.Interaction):
    """Worked out from permissions and bindings, never from a list."""
    guild = interaction.guild
    visible = _visible_rooms(guild)
    invisible = [c for c in guild.text_channels if c not in visible]
    answers = [c for c in visible if brain.may_speak_in(guild, c)]
    lines = [
        f"## What Eugene can see in {guild.name}",
        f"**Can read**: {len(visible)} of {len(guild.text_channels)} "
        f"channels. Discord decides this, not him: it is wherever his role "
        f"has Read Messages.",
        f"**Cannot see at all**: {len(invisible)}"
        + (": " + ", ".join(c.mention for c in invisible[:10])
           + (" and more" if len(invisible) > 10 else "")
           if invisible else ""),
        "",
    ]
    if not module_live(guild, "chat"):
        lines += [
            "**Listens in / answers in**: nowhere. Conversation is off "
            "here, so he reads nothing and keeps no transcript at all.",
        ]
    else:
        lines += [
            "**Answers in, and therefore listens in**: "
            + (", ".join(c.mention for c in answers[:10])
               + (" and more" if len(answers) > 10 else "")
               if answers else "nowhere"),
            "-# One condition, not two: the room he may answer in is the "
            "only room he remembers. A channel he is kept out of is one he "
            "learns nothing from.",
        ]
    lines += [
        "",
        "-# He does note that you were around: your id and a timestamp, in "
        "every channel he can see: because that is what the away rule "
        "counts. No message text, and it never leaves the host.",
    ]
    await interaction.response.send_message("\n".join(lines)[:1990],
                                            ephemeral=True)


@bot.tree.command(name="privacy",
                  description="What leaves this server, and where it goes")
@app_commands.guild_only()
async def slash_privacy(interaction: discord.Interaction):
    """Anybody in the room, not just an administrator."""
    guild = interaction.guild
    provider = brain.provider_name(guild.id)
    lines = [f"## What leaves {guild.name}"]
    if provider is None:
        lines += [
            "**Nothing.** No AI key is set here, so no message, name or "
            "proposal is sent anywhere. Votes, the record, the roster and "
            "the reminders are all worked out on the host and stay there.",
        ]
    else:
        held = brain.holding(guild)
        lines += [
            f"**{providers.label(provider)}** is on duty, on "
            f"`{brain.model_name(guild.id)}`, paid for by this server.",
            "",
            "When somebody in the cooperative talks to him, one request "
            "goes out carrying:",
            f"- up to **{held['message_cap']} recent messages** from that "
            f"room, with display names",
            f"- up to **{held['result_cap']} tool results** from that room",
            "- the roster count and what a vote needs today",
            "- which features are on, this server's name and description",
            "",
            f"Right now he is holding **{held['messages']} message(s)** and "
            f"**{held['tool_results']} tool result(s)** for this server, in "
            f"memory only. A restart clears them; so does **Purge** below.",
            "",
            "**Never sent:** how anybody voted, anything about people who "
            "are not in the room, or any durable note about a person -- he "
            "keeps none, so there is nothing to send. Nothing goes out on a "
            "timer; he speaks when spoken to.",
            "",
            f"-# ${brain.spend_usd(guild.id):.2f} of "
            f"${settings.budget_usd(guild.id):.0f} spent this month. Full "
            f"detail, with the line of code that does each part, is in "
            f"PRIVACY.md in the repository.",
        ]
    view = PurgeView() if provider is not None else None
    await interaction.response.send_message(
        "\n".join(lines)[:1990], view=view, ephemeral=True
    )


class PurgeView(discord.ui.View):
    """Anybody's, deliberately. What it drops is the room's recent
    conversation as he is holding it, which is everybody's, so making it an
    administrator's button would be protecting the wrong person."""

    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Forget what you are holding",
                       style=discord.ButtonStyle.danger)
    async def purge(self, interaction, button):
        gone = brain.forget_here(interaction.guild)
        await interaction.response.edit_message(
            content=f"Dropped: {gone['messages']} message(s) and "
                    f"{gone['tool_results']} tool result(s). The record, the "
                    f"roster and the votes are untouched -- this was only "
                    f"what I had in my head.",
            view=None,
        )


def house_body(guild):
    """The whole of `/house` as one piece of text, so the bell can redraw
    the screen without anybody running the command a second time."""
    chosen = len(settings.voting_overrides(guild.id))
    tail = ("\n-# These are all the defaults so far: just tell me what you "
            "want changed." if not chosen else
            f"\n-# {chosen} of these numbers are yours; the rest are the "
            f"ones he came with.")
    return (
        f"## What {guild.name} has switched on\n"
        + module_summary(guild)
        + "\n\n## What it votes by\n"
        + "\n".join(voting_lines(guild))
        + tail
    )


class BellButton(discord.ui.Button):
    """Asking to be told about new votes, and asking him to stop.

    Here rather than in `#votes`, which is ballots and nothing else: a
    standing card offering notifications is read once and scrolled past for
    ever, and it spends the rest of its life sitting between somebody and
    the vote they came in to cast. This screen is already the one a member
    opens to find out how their house works, it costs nothing to draw, and
    the answer it gives changes with the press.
    """

    def __init__(self, holding):
        super().__init__(
            label="Stop telling me about new votes" if holding
            else "Tell me when a vote opens",
            style=discord.ButtonStyle.secondary,
        )
        self.holding = holding

    async def callback(self, interaction):
        guild, you = interaction.guild, interaction.user
        bell = bell_role(guild)
        if bell is None:
            return await interaction.response.edit_message(
                content=house_body(guild)
                + "\n-# There is no bell here to pick up. `/setup` → Apply "
                  "makes one.",
                view=None,
            )
        try:
            if self.holding:
                await you.remove_roles(bell, reason="/house: no more ringing")
            else:
                await you.add_roles(bell, reason="/house: ring me")
        except discord.HTTPException as e:
            return await interaction.response.edit_message(
                content=house_body(guild)
                + f"\n-# Could not: {e!r}: my role has to sit above "
                  f"`{bell.name}` in the list.",
                view=house_view(guild, self.holding),
            )
        # Told what changed from what was pressed, not from `you.roles`:
        # Discord tells him about the new roles down the gateway a moment
        # later, so reading them back here answers with the old ones.
        note = ("\n-# Done. No vote will mention you again; the floor is "
                "still there whenever you want to look."
                if self.holding else
                "\n-# Done. A new vote will mention you once, on the ballot "
                "itself. Nothing else will.")
        await interaction.response.edit_message(
            content=house_body(guild) + note,
            view=house_view(guild, not self.holding),
        )


def house_view(guild, holding):
    """The bell, where there is one to press.

    A house that has switched the pings off, or has no bell role at all,
    gets no button and no explanation of a button it has not got.
    """
    if not ringing(guild) or bell_role(guild) is None:
        return None
    view = discord.ui.View(timeout=600)
    view.add_item(BellButton(holding))
    return view


@bot.tree.command(name="house", description="What Eugene is running for this server")
@app_commands.guild_only()
async def slash_house(interaction: discord.Interaction):
    """What is switched on and what this house votes by, free and unchanged
    by reading it.

    The point of the command is that it costs nothing and needs no key: a
    house whose brain is dormant, or whose bill has run out for the month,
    can still read its own numbers. A rule you cannot read is a rule nobody
    trusts. Changing one is done by asking him, or on the panel.

    The one button on it changes nothing about the house. It is where you
    say whether you personally want telling when a vote opens, which is the
    same question this screen answers about everything else and the only
    one on it whose answer is yours alone.
    """
    if not keyed_in(interaction):
        return await refuse(interaction, "That one is for people who are in.")
    guild = interaction.guild
    bell = bell_role(guild)
    view = house_view(guild, bell is not None and bell in interaction.user.roles)
    await interaction.response.send_message(
        house_body(guild), ephemeral=True,
        **({"view": view} if view is not None else {}),
    )


# ---------- which Claude does the talking ----------
# The one typed command here that is not member-tier. Everything else in
# this section is a second door onto something the buttons already gave
# everybody; this one moves the bill. Opus is five times haiku on the way in
# and five times on the way out, so the person who chose it should be the
# person who answers for the month, which is the same person `/setup` lets
# in. The rest of the setup screen stays where it is: this is the one knob
# worth a command of its own, because it is the one a house changes twice a
# week rather than once ever.

CLAUDE_TIER_BLURB = {
    "haiku": "cheap and quick: the one he came with",
    "sonnet": "the middle rung, three times haiku",
    "opus": "the good one, five times haiku",
}


def _claude_tier_choices():
    """The rungs Claude names, as Discord choices. Built from providers.py
    rather than typed twice, so a fourth tier is one line over there."""
    return [
        app_commands.Choice(
            name=f"{tier}: {CLAUDE_TIER_BLURB.get(tier, model)}", value=tier
        )
        for tier, model in providers.tiers("claude").items()
    ]


@bot.tree.command(
    name="model", description="Switch which Claude does the talking"
)
@app_commands.describe(tier="haiku, sonnet or opus. Leave it out to see where he is.")
@app_commands.choices(tier=_claude_tier_choices())
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def slash_model(interaction: discord.Interaction,
                      tier: app_commands.Choice[str] = None):
    guild = interaction.guild
    if not is_steward(interaction):
        return await refuse(interaction, STEWARD_ONLY)
    if not settings.brain_key(guild.id, "claude"):
        return await refuse(
            interaction,
            "There is no Claude key here, so there is nothing to switch "
            "between. `/setup` → **Brain** is where one goes.",
        )
    tiers = providers.tiers("claude")
    now = settings.model(guild.id, "claude", providers.default_model("claude"))
    standing = providers.tier_of("claude", now)

    if tier is None:
        # Asking is free and answering costs nothing, so the no-argument
        # form is a straight read: what he is on, and what else there is.
        rungs = "\n".join(
            f"- **{name}**: `{model}`" + (" ← here" if model == now else "")
            for name, model in tiers.items()
        )
        return await refuse(
            interaction,
            f"Claude is on `{now}`"
            + (f" ({standing})." if standing else
               ": not one of the named rungs, so it stays untouched "
               "unless you pick one.")
            + f"\n{rungs}\n-# `/model tier: opus` moves him.",
        )

    wanted = tiers[tier.value]
    if wanted == now and brain.provider_name(guild.id) == "claude":
        return await refuse(interaction, f"He is already on `{wanted}`.")

    # Checked against this server's own key before it is stored: a key on a
    # plan that cannot reach opus should fail here, once, rather than on the
    # next thing somebody says to him.
    await interaction.response.defer(ephemeral=True, thinking=True)
    problem = await brain.validate_key(
        "claude", settings.brain_key(guild.id, "claude"), wanted
    )
    if problem:
        return await interaction.followup.send(
            f"Nothing changed, he stays on `{now}`. {problem}", ephemeral=True
        )

    settings.set_model(guild.id, "claude", wanted)
    # Choosing a Claude and leaving Gemini on duty would be a setting that
    # changes nothing, which is worse than a refusal. Whoever ran this meant
    # to be answered by that model, so Claude comes on duty with it -- said
    # out loud below, because it is a bigger move than the one they typed.
    was = brain.provider_name(guild.id)
    if was != "claude":
        settings.set_provider(guild.id, "claude")
    brain.forget_client(guild.id)
    price_in, price_out = providers.prices("claude", wanted)
    log.info(
        f"guild {guild.id} put Claude on {wanted} "
        f"({interaction.user.display_name})"
    )
    await interaction.followup.send(
        f"Done. He talks through Claude on `{wanted}`.\n"
        + (f"-# That takes {providers.label(was)} off duty; its key stays on "
           f"file and the **Brain** screen puts it back.\n" if was != "claude"
           else "")
        + f"-# ${price_in:.2f} in and ${price_out:.2f} out per million, and "
          f"the counter now bills at that rate. "
          f"${brain.spend_usd(guild.id):.2f} of "
          f"${settings.budget_usd(guild.id):.0f} spent this month.",
        ephemeral=True,
    )


# ---------- pinned buttons ----------

async def ensure_button_message(guild, channel, state_key, content, view,
                                restamp=False):
    if channel is None:
        log.warning(f"channel for {state_key} missing; button not posted")
        return
    state = load_json(state_path(guild), {})
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
    save_json(state_path(guild), state)
    log.info(f"button posted in #{channel.name}")


# ---------- health endpoint ----------
# Binds only when PORT is set (Render provides it). Serves /healthz with
# read-only vitals; the future dashboard grows from here.

async def start_web():
    port = int(os.environ.get("PORT", 0))
    if not port:
        return

    async def healthz(request):
        ready = bot.is_ready()
        served = houses() if ready else []
        open_bills = 0
        acts = 0
        for guild in served:
            open_bills += sum(
                1 for b in load_json(bills_path(guild), [])
                if b.get("status") == "on_floor"
            )
            acts += len(load_json(acts_path(guild), []))
        return web.json_response(
            {
                "status": "ok",
                "commit": COMMIT,
                "clerk": str(bot.user) if ready else None,
                "servers": len(served),
                "ready": ready,
                "latency_ms": round(bot.latency * 1000) if ready else None,
                "open_bills": open_bills,
                "acts": acts,
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
    # Before the harness, and that order is load-bearing now: `toolbox`
    # folds any old memory book into this store on the way up, and a store
    # that has not been told where it lives accepts the writes and drops
    # them on the floor.
    toolbox.configure(
        HERE, DATA,
        {**BILL_ACTIONS, **DUTY_ACTIONS, **POLL_ACTIONS,
         **powers.ACTIONS_TABLE},
        in_cooperative=in_cooperative, numbers=numbers,
        # The same question the setup panel's own check asks, so rewriting
        # the configuration by asking is shut to exactly the people it is
        # shut to by pressing.
        is_steward=is_admin,
    )
    # The officer's hands. in_cooperative goes in twice on purpose: the
    # harness uses it to decide who may reach the elevated tools at all,
    # and powers.py uses it to decide who those tools may be used on.
    powers.configure(bot, in_cooperative, health_log)
    settings.configure(DATA)
    # server_config.yaml's window becomes the default every house starts
    # from, and stops being the rule the moment one sets its own.
    settings.configure_voting(floor_hours=CONFIG_FLOOR_HOURS)
    roster.configure(DATA)
    duties.configure(DATA)
    brain.configure(
        bot, HERE, DATA, in_cooperative, health_log, chunk_text, resolve_guild,
        numbers=numbers,
    )
    if not intents.message_content:
        log.warning(
            "running without the Message Content intent: Eugene cannot "
            "hear anything, whatever keys a server sets: and the filters "
            "read an empty message, so automod is off in fact whatever the "
            "settings say"
        )
    bot.add_view(SubmitBillView())
    bot.add_view(Card(rows=[BallotRow()]))
    bot.add_view(Card(rows=[MemberBallotRow()]))
    # Dummy labels: this instance exists only to route clerk:opt_* presses
    # after a restart. Without it a deploy mid-vote leaves every choice
    # ballot on the floor with dead buttons and no way to say so.
    bot.add_view(Card(rows=MultiBallotRows()))
    bot.add_view(NotesView())
    bot.add_view(Card(rows=[VetoRow()]))
    # The same routing problem the ballots have, and one more: a deploy
    # between somebody being shown a confirmation and pressing it must
    # leave the button working, or the question they typed is gone with no
    # way to say so.
    bot.add_view(Card(rows=[PollRow()]))
    bot.add_view(Card(rows=PollChoiceRows()))
    bot.add_view(PollConfirm())
    # One or the other, never both. A command registered globally *and*
    # to a guild shows up twice in that guild's picker, and both copies
    # persist -- so doing both to be safe is the one option that is
    # visibly wrong to everybody in the server.
    #
    # GUILD_ID means "just this server, and now": a guild sync appears
    # immediately where a global one takes Discord up to an hour. Unset it
    # to serve every server he is invited to.
    if DEV_GUILD_ID:
        where = discord.Object(id=DEV_GUILD_ID)
        bot.tree.copy_global_to(guild=where)
        await bot.tree.sync(guild=where)
        log.info(f"commands synced to guild {DEV_GUILD_ID} only "
                 f"(GUILD_ID is set); unset it to publish globally")
    else:
        await bot.tree.sync()
        log.info("commands synced globally; Discord may take up to an hour")


async def ensure_furniture(guild, restamp=False):
    """Post any missing furniture. Runs at boot (restamp=True, so
    deploys refresh buttons and wording) and periodically (verify only)."""
    if module_live(guild, "governance"):
        await ensure_button_message(
            guild,
            room(guild, "proposals"),
            "bill_message_id",
            "*Say what should change, and why. Eugene files it and the "
            "cooperative votes. Authorship is public; rules do not have "
            "anonymous authors.*",
            SubmitBillView(),
            restamp=restamp,
        )


@tasks.loop(seconds=300)
async def furniture_loop():
    for guild in houses():
        # Clear bindings whose channel or role has been deleted, so a room
        # that quietly went away shows as unbound instead of as silence.
        bindings.prune(guild)
        try:
            await ensure_furniture(guild)
        except Exception as e:
            log.error(f"furniture check failed in {guild.id}: {e!r}")


# Ran once per server, on the upgrade that flipped conversation's default.
CHAT_MIGRATED = "chat_default_migrated"


def keep_talking(guild):
    """A server that was already talking does not go quiet on an upgrade.

    Conversation used to default on and dormant; it defaults off now,
    because a feature that is on and waiting on a key reads as broken when
    nothing is. That is right for a new install and wrong for every server
    already running: they never chose `chat` explicitly, so the new
    default would silence a clerk that had been answering for months, on a
    deploy nobody connected to it.

    So: a server holding an AI key was talking, and keeps talking.

    Once, and marked, because "never chose" and "chose the default" are the
    same stored value: `set_enabled` drops an entry that matches the
    default, so a house that deliberately switched conversation off looks
    exactly like one that never touched it. Inferring again on every boot
    would switch them back on every deploy, for ever.
    """
    if settings.get(guild.id, CHAT_MIGRATED):
        return
    settings.put(guild.id, **{CHAT_MIGRATED: True})
    if brain.provider_name(guild.id) is None:
        return
    modules.set_enabled(guild.id, "chat", True)
    log.info(f"guild {guild.id}: conversation kept on, since a key is set "
             f"and it was talking before this upgrade")


@bot.event
async def on_ready():
    log.info(f"on duty as {bot.user} in {len(bot.guilds)} server(s) "
             f"(commit {COMMIT})")
    first = not getattr(bot, "_boot_announced", False)
    bot._boot_announced = True
    for guild in houses():
        try:
            keep_talking(guild)
            # A host that still carries a key in its environment hands it to
            # each server it serves, once, and is never read again.
            adopted = settings.adopt_env_keys(guild.id)
            if adopted:
                log.info(f"adopted host {', '.join(adopted)} key(s) into "
                         f"guild {guild.id}")
            await ensure_furniture(guild, restamp=first)
            if first:
                await repaint_cards(guild)
                state = load_json(state_path(guild), {})
                if state.get("announced_commit") != COMMIT:
                    state["announced_commit"] = COMMIT
                    save_json(state_path(guild), state)
                    await health_log(guild, f"🟢 On duty. Commit `{COMMIT}`.")
        except Exception as e:
            log.error(f"could not settle into {guild.id}: {e!r}")
    if not check_floor.is_running():
        check_floor.start()
    if not check_polls.is_running():
        check_polls.start()
    if not furniture_loop.is_running():
        furniture_loop.start()
    if not duty_loop.is_running():
        duty_loop.start()


def watched_invites(guild):
    """Passed invitations whose link is still worth watching: issued, inside
    a window that could still take it back, and nobody attributed to it yet."""
    return [
        b for b in load_json(bills_path(guild), [])
        if b.get("kind") == "invite" and b.get("invite_code")
        and not b.get("joined_id") and veto_open(b)
    ]


@bot.event
async def on_member_join(member: discord.Member):
    """Which door somebody came through, while it still matters.

    Asked only when a link Eugene issued is inside a window that could
    still take it back, and only to answer one question: whether revoking
    that link also means seeing the person who used it out again.

    A single-use invite leaves Discord the moment it is spent, so a code
    that was there and is not is the one they came in on. If two vanished
    between one join and the next, nothing is written down: guessing here
    removes the wrong person, and the veto says plainly that it could not
    tell rather than acting on a coin flip.
    """
    guild = member.guild
    if guild is None or not serves(guild) or member.bot:
        return
    if not module_live(guild, "governance"):
        return
    watched = watched_invites(guild)
    if not watched:
        return
    try:
        live = {invite.code for invite in await guild.invites()}
    except discord.HTTPException as e:
        # Reading the server's invites wants Manage Server, which he is not
        # always given. Without it the veto falls back to the id on the
        # proposal, if the proposer knew one.
        log.warning(f"could not read invites in {guild.id}: {e!r}")
        return
    spent = [b for b in watched if b["invite_code"] not in live]
    if len(spent) != 1:
        return
    bill = bill_by(guild, "no", spent[0]["no"])
    if bill is None:
        return
    bill["joined_id"] = member.id
    await update_bill(guild, bill)
    log.info(f"guild {guild.id}: {member.id} arrived on the link from "
             f"proposal no. {bill['no']}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    # Being around is what keeps you on the roster; nobody should have to
    # file paperwork to prove they still exist.
    if message.guild is not None:
        roster.touch(message.guild.id, message.author.id)
    # Conversation off means he does not read the room at all, which is
    # also what stops him learning people: `memory` stands on `chat` for
    # exactly this reason. Resolved the way brain.py resolves it, because a
    # direct message has no guild of its own and a switch every room
    # respects except that one is not a switch.
    if not module_live(message.guild or resolve_guild(message), "chat"):
        return
    try:
        await brain.handle_message(message)
    except Exception as e:
        log.error(f"brain handler error: {e!r}")


if __name__ == "__main__":
    logging.getLogger("discord").setLevel(logging.WARNING)
    bot.run(TOKEN, log_level=logging.INFO)
