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
Voting:     thresholds count against the roster, not against turnout, so a
            vote ends the moment its result is settled rather than when the
            clock runs out. A majority carries most things; a removal wants
            three quarters. voting.floor_hours is only the backstop for a
            vote nobody finishes.
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
import powers
import providers
import roster
import sanction
import settings
import slate
import toolbox
import warden

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

# Both at once, and said in words. A KeyError out of the top of the file is
# a true answer to a question nobody asked: the host gets a traceback about
# os.environ, a restart policy obligingly produces ten more of it, and
# nothing anywhere says which variable, what goes in it, or where it is
# set. It costs four lines to say that instead.
_MISSING = [n for n in ("DISCORD_TOKEN", "GUILD_ID")
            if not (os.environ.get(n) or "").strip()]
if _MISSING:
    raise SystemExit(
        f"Eugene cannot start: {' and '.join(_MISSING)} "
        f"{'is' if len(_MISSING) == 1 else 'are'} not set.\n"
        f"On a host these are the service's environment variables; on a "
        f"laptop they live in a .env file next to clerk.py, and "
        f"`python install.py` writes one for you.\n"
        f"DISCORD_TOKEN is the bot token from the Discord developer portal. "
        f"GUILD_ID is the id of the server he keeps -- turn on Developer "
        f"Mode, right-click the server icon, Copy Server ID."
    )
TOKEN = os.environ["DISCORD_TOKEN"].strip()
try:
    GUILD_ID = int(os.environ["GUILD_ID"].strip())
except ValueError:
    raise SystemExit(
        f"Eugene cannot start: GUILD_ID is "
        f"{os.environ['GUILD_ID'].strip()!r}, which is not a server id. It "
        f"is the long number Copy Server ID gives you, digits only -- not "
        f"the server's name and not an invite link."
    )

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

STATE = DATA / "clerk_state.json"
BILLS = DATA / "bills.json"
ACTS = DATA / "acts.json"


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
    "Only the cooperative files proposals here — whoever has picked up a "
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


def archive_category(guild):
    return next((c for c in guild.categories if "archive" in c.name.lower()), None)


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
        f"Vote window: {numbers(guild)['floor_hours']:g}h",
        brain.status_line(guild.id),
        # Only when there is something waiting. A card in the log room is
        # easy to scroll past, and a sign-off nobody notices is a
        # moderation action that silently never happened -- which is the
        # one way this whole arrangement fails quietly.
        *([f"⏳ {waiting}"] if (waiting := sanction.summary(guild.id)) else []),
        f"-# Updated <t:{int(now_utc().timestamp())}:R>. "
        f"Administrators only.",
    ]
    return "\n".join(lines)


async def update_health(guild):
    if not modules.enabled(guild.id, "health"):
        return
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
COOPERATIVE_ONLY = "cooperative"
EVERYONE = "everyone"


def audience_of(bill):
    return EVERYONE if bill.get("audience") == EVERYONE else COOPERATIVE_ONLY


def belongs_to(bill):
    """The membership test a ballot admits people by.

    One function, handed to both the door and the arithmetic, because those
    two disagreeing is the failure that cannot be seen from outside: a vote
    counted against a roster wider than the one allowed to vote in it can
    never pass, and one counted against a narrower roster passes on fewer
    people than it claims. So `may_vote` and the denominator are the same
    question asked twice, never two questions that happen to agree.
    """
    return in_room if audience_of(bill) == EVERYONE else in_cooperative


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

    The cooperative's business goes to the floor. A poll open to everyone
    goes where everyone can see it, which is the polls room -- putting an
    open poll on a floor the room cannot read would be a vote nobody it was
    open to could find, and the permissions would make a liar of the word
    "open" while the arithmetic went on counting them.
    """
    if audience_of(bill) == EVERYONE:
        return room(guild, "polls")
    return room(guild, "votes")


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
    """Everything the ballot needs to describe itself right now.

    One shape for every kind of vote. A choice ballot fills in `options`,
    `counts`, `leader` and `leaders` on top of the common turnout figures;
    a yes/no ballot leaves `options` empty. Anything describing a vote in
    words -- the ballot itself, the receipt, the nudge -- branches on that
    one key rather than keeping a second set of sums of its own.

    `open_kind` is the other branch, and it decides what the numbers even
    mean. A vote the cooperative takes is carried against the roster, so
    `need` is a count of yes votes and not voting is a no. A poll open to
    the whole server is carried by a majority of whoever voted, so `need`
    is meaningless there and `quorum` -- how many have to turn up at all --
    is the number doing the work. Nothing reads both.
    """
    ballots = bill.get("ballots", {})
    roll = electorate(guild, bill)
    size = len(roll)
    tier = vote_tier(bill)
    open_kind = audience_of(bill) == EVERYONE
    figures = numbers(guild)
    yes = sum(1 for v in ballots.values() if v == "yes")
    no = sum(1 for v in ballots.values() if v == "no")
    abstain = sum(1 for v in ballots.values() if v == "abstain")
    st = {
        "size": size,
        "tier": tier,
        "audience": audience_of(bill),
        "open_kind": open_kind,
        "need": 0 if open_kind else roster.required(
            size, tier, figures["fundamental_share"]
        ),
        "quorum": roster.quorum(size, figures["public_quorum_share"]) if open_kind else 0,
        "yes": yes,
        "no": no,
        "abstain": abstain,
        "voted": len(ballots),
        "waiting": max(size - len(ballots), 0),
        "options": bill.get("options") or None,
        "counts": {},
        "leader": 0,
        "leaders": [],
        "clinch": 0,
        "round": bill.get("round", 1),
    }
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
        # where it stands.
        st["clinch"] = size // 2 + 1
    return st


def bar(done, total, width=10):
    total = max(total, 1)
    filled = max(0, min(width, round(width * done / total)))
    return "█" * filled + "░" * (width - filled)


def choice_body(st):
    """The live face of a choice ballot: one bar per option, measured
    against the count that would settle it outright.

    A choice ballot is a vote about a thing, so it shows everything, for
    the same reason `is_blind` gives -- hiding the standing on a question
    about a channel name is ceremony, and a running count is what tells
    people whether their option is worth arguing for while there is still
    time to argue.
    """
    rule = (
        "this runoff goes to whichever leads at close"
        if st["round"] > 1
        else "otherwise a majority of votes cast at close, or a runoff"
    )
    rows = "\n".join(
        f"`{bar(n, st['clinch'])}` **{o}** — {n}" for o, n in st["counts"].items()
    )
    return (
        f"🗳️ **{st['voted']} of {st['size']} voted**\n{rows}\n"
        f"-# {st['clinch']} carries an option outright; {rule}."
    )


def open_body(st):
    """The live face of a poll open to the whole server.

    Two things to show and they are not the same thing: whether enough of
    the room has turned up for the poll to count at all, and which way the
    people who did turn up are leaning. The bar measures the first, because
    that is the one anybody reading can still do something about.
    """
    short = max(st["quorum"] - st["voted"], 0)
    standing = (
        f"{short} more {'vote' if short == 1 else 'votes'} and this poll counts"
        if short
        else "Quorum met; this poll counts."
    )
    return (
        f"🗳️ `{bar(st['voted'], st['quorum'])}` **{st['voted']} of "
        f"{st['size']} voted** · quorum **{st['quorum']}**\n"
        f"✅ {st['yes']}  ·  ❌ {st['no']}"
        + (f"  ·  🤍 {st['abstain']}" if st["abstain"] else "")
        + f"\n-# {standing} Open to the whole server, and carried by a "
        f"majority of whoever votes — not voting is not a no here."
    )


def ballot_content(guild, bill):
    """The live face of a vote, whatever shape it is. Rewritten on every
    ballot cast, so the progress toward the threshold is visible the whole
    way rather than arriving as a surprise at close."""
    st = vote_state(guild, bill)
    chamber = chamber_of(guild, bill)
    where = f" · debate in {chamber.mention}" if chamber else ""
    try:
        ends = datetime.fromisoformat(bill["ends_at"])
        clock = f" · closes <t:{int(ends.timestamp())}:R>"
    except (KeyError, ValueError):
        clock = ""

    round_note = " (runoff)" if bill.get("round", 1) > 1 else ""
    # Both markers are said out loud because both are claims on other
    # people: an open poll is a claim on the whole server's attention, and
    # priority is a claim on their inbox. Neither should be discoverable
    # only by being direct-messaged, or by noticing you were not.
    kind_note = ""
    if audience_of(bill) == EVERYONE:
        kind_note = " · 📣 open to everyone"
    elif is_priority(bill):
        kind_note = " · ⚡ priority"
    head = (f"**Proposal No. {bill['no']}: {bill['title']}**"
            f"{round_note}{kind_note}{where}{clock}")
    if st["options"]:
        body = choice_body(st)
    elif is_blind(bill):
        body = (
            f"🗳️ `{bar(st['voted'], st['size'])}` **{st['voted']} of "
            f"{st['size']} voted** · needs **{st['need']} yes**\n"
            f"-# No running count on a vote about a person."
        )
    elif st["open_kind"]:
        body = open_body(st)
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
    if guild is None or bill.get("status") != "on_floor":
        return
    floor = floor_for(guild, bill)
    if floor is None or not bill.get("ballot_message_id"):
        return
    try:
        msg = await floor.fetch_message(bill["ballot_message_id"])
        await msg.edit(content=ballot_content(guild, bill))
    except discord.HTTPException as e:
        log.warning(f"could not repaint ballot for bill {bill['no']}: {e!r}")


async def repaint_open_ballots(guild):
    """Redraw every vote still on the floor. Runs once at boot, alongside
    the furniture restamp and for the same reason: a deploy that changes
    how a ballot describes itself should reach the votes already open,
    not just the next one filed."""
    for bill in load_json(BILLS, []):
        if bill.get("status") == "on_floor":
            await refresh_ballot(guild, bill)


def vote_settled(st):
    """Whether a vote's result can still change.

    Passing is settled the instant enough yes votes exist: the threshold is a
    share of the roster, not of turnout, so once it is met no later ballot can
    take it back. Failing waits for everyone, because a no can still become a
    yes while the vote is open -- an unreachable threshold is only genuinely
    unreachable once nobody is left to change their mind.

    A choice ballot is settled once an option is past half the roster --
    that is a majority of whoever ends up voting, however many that is --
    and otherwise waits for the room, since the leader can still change
    hands while the vote is open.

    An open poll is settled when its lead is bigger than the number of
    people left to vote, once enough of them have voted to count it. It is
    carried on a majority of votes cast, so there is no threshold to reach
    early -- but there is still a point past which nobody left can change
    the answer, and that is the same point, arrived at from the other
    direction. Both halves are needed: a lead of ten with quorum unmet is
    not decided, because the poll could still fail for want of turnout.

    An empty roster is never settled: that means we cannot see who is here,
    not that nobody is.
    """
    if st["size"] <= 0:
        return False
    if st["voted"] >= st["size"]:
        return True
    if st["options"]:
        # An option past half the room has a majority of however many end
        # up voting, whoever else turns up. On an open poll it still has to
        # have brought the quorum with it.
        if st["open_kind"] and st["voted"] < st["quorum"]:
            return False
        return st["leader"] >= st["clinch"]
    if st["open_kind"]:
        left = st["size"] - st["voted"]
        return st["voted"] >= st["quorum"] and abs(st["yes"] - st["no"]) > left
    return st["yes"] >= st["need"]


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
    await update_bill(bill)
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
    if st["open_kind"]:
        # Never "x more carries it": on a majority of votes cast there is no
        # such number, and inventing one would be a lie told to get a click.
        short = max(st["quorum"] - st["voted"], 0)
        if short:
            return (f"{st['voted']} of {st['size']} have voted; {short} more "
                    f"and it counts.")
        if st["yes"] == st["no"]:
            return f"{st['voted']} have voted and it is level."
        ahead = "yes" if st["yes"] > st["no"] else "no"
        return (f"{st['voted']} of {st['size']} have voted, and {ahead} leads "
                f"{max(st['yes'], st['no'])} to {min(st['yes'], st['no'])}.")
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
    await interaction.response.send_message(
        f"Your ballot: **{choice}**. {note} "
        f"{standing_line(interaction.guild, bill, carried='That carries it.')} "
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
    the message itself.

    The buttons are the only thing here that differs from a yes/no ballot.
    Everything a press then does -- who may vote, recording it, repainting
    the message, ending the vote once it is decided -- is `cast_ballot`,
    the same as every other ballot in the server."""

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
            await cast_ballot(interaction, index=index)
        return callback

    async def _retract(self, interaction):
        await cast_ballot(interaction)


async def file_bill(guild, author, title, what, why, kind="ordinary",
                    options=None, target_id=None, floor_hours=None,
                    eligible_ids=None, audience=COOPERATIVE_ONLY,
                    priority=False):
    """Shared filing pipeline for all proposal kinds. Returns the filed
    proposal,
    or None if the room it belongs in is missing. Callers own their
    acknowledgement, since Eugene also files on request in conversation,
    where there is no interaction to reply to.

    `audience` decides who votes and, from that, everything else: which
    room it is posted in, who the debate on it is open to, what carries
    it. It defaults to the closed kind, because a caller that forgets to say has
    said nothing, and the wrong default there is the one that puts the
    cooperative's business in front of the whole server.
    """
    audience = EVERYONE if audience == EVERYONE else COOPERATIVE_ONLY
    floor = floor_for(guild, {"audience": audience})
    if floor is None:
        return None
    number = await next_bill_number()
    ends_at = now_utc() + timedelta(
        hours=floor_hours or numbers(guild)["floor_hours"]
    )

    stamp = await floor.send(
        view=Card([f"## Proposal No. {number}: {title}\nSubmitted by {author.mention}"])
    )
    # One thread per proposal, and it hangs off the proposal itself. There
    # was briefly a second one beside it for the argument, which meant two
    # rooms in the sidebar for one vote and a guess to make before typing
    # about which of them a thought belonged in. A week, because a vote runs
    # for days and a thread that files itself away mid-argument reads as the
    # argument being over.
    notes_thread = await stamp.create_thread(
        name=(bill_name(number, title) + ": notes")[:100],
        auto_archive_duration=10080,
    )
    notes_msg = await notes_thread.send(NOTES_PROMPT, view=NotesView())
    for label, body in (("What", what), ("Why", why)):
        for i, piece in enumerate(chunk_text(body)):
            prefix = f"### {label}\n" if i == 0 else ""
            await floor.send(prefix + piece)
    record = {
        "no": number,
        "title": title,
        "kind": kind,
        "audience": audience,
        "priority": bool(priority),
        "target_id": target_id,
        "eligible_ids": eligible_ids,
        "author_id": author.id,
        "author": author.display_name,
        "what": what,
        "why": why,
        "message_id": stamp.id,
        "notes_message_id": notes_msg.id,
        "notes_thread_id": notes_thread.id,
        "submitted_at": now_utc().isoformat(),
        "ends_at": ends_at.isoformat(),
        "status": "on_floor",
        "options": options or None,
        "round": 1,
        "ballots": {},
        "notes": {},
    }
    # The buttons are the only thing that varies by kind. The message above
    # them is the same live face for every vote, painted from the proposal
    # itself, so a ballot shows what it needs from its first second rather
    # than only once somebody has voted.
    if options:
        view = MultiBallotView(options)
    elif kind == "invite":
        view = MemberBallotView()
    else:
        view = BallotView()
    ballot = await floor.send(ballot_content(guild, record), view=view)
    record["ballot_message_id"] = ballot.id

    bills = load_json(BILLS, [])
    bills.append(record)
    save_json(BILLS, bills)
    log.info(f"proposal filed: no. {number} ({title!r}, {kind}) by {author.display_name}")
    return record


async def file_from_modal(interaction, **kwargs):
    """Filing from a button, with the ephemeral receipt that expects."""
    bill = await file_bill(interaction.guild, interaction.user, **kwargs)
    if bill is None:
        missing = ("polls" if kwargs.get("audience") == EVERYONE else "votes")
        return await interaction.followup.send(
            f"There is no room bound for the `{missing}` job, so there is "
            f"nowhere to put this. An admin can point me at one with "
            f"`/setup`.",
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


class PollModal(discord.ui.Modal, title="Open a public poll"):
    """A question put to the whole server rather than to the cooperative.

    Deliberately the same four fields as a proposal, because it is the same
    thing asked of different people, and a second form that looked
    different would suggest the difference was in the asking rather than in
    who is asked.
    """

    poll_title = discord.ui.TextInput(
        label="Title",
        style=discord.TextStyle.short,
        placeholder="A short name for it.",
        max_length=100,
    )
    what = discord.ui.TextInput(
        label="What",
        style=discord.TextStyle.paragraph,
        placeholder="The question. This is the text people vote on.",
        max_length=4000,
    )
    why = discord.ui.TextInput(
        label="Why",
        style=discord.TextStyle.paragraph,
        placeholder="Why you are asking. Worth a line even for a poll.",
        max_length=4000,
    )
    choices = discord.ui.TextInput(
        label="Options (empty = yes/no ballot)",
        style=discord.TextStyle.paragraph,
        placeholder="One option per line, 2 to 10 lines.",
        required=False,
        max_length=800,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            options = parse_options(self.choices)
        except ValueError as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        await file_from_modal(
            interaction,
            title=str(self.poll_title),
            what=str(self.what),
            why=str(self.why),
            options=options,
            audience=EVERYONE,
        )


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
        id_part = f" (Discord ID {str(self.discord_id).strip()})" if str(self.discord_id).strip() else ""
        what = (
            f"{name}{id_part} shall be invited to {interaction.guild.name}. "
            f"If this passes, Eugene issues a single-use invite link, valid "
            f"seven days, delivered privately to the proposer. It is a place "
            f"in the room; it is not a place in the cooperative and not a "
            f"vote."
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
        label="Open a public poll",
        emoji="📣",
        style=discord.ButtonStyle.secondary,
        custom_id="clerk:bill_poll",
    )
    async def poll(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Opening one is the cooperative's, per the standing orders; voting
        # in it is everybody's, which is the whole point of the thing.
        if not self._keyed(interaction):
            return await interaction.response.send_message(
                NOT_INSIDE, ephemeral=True
            )
        if room(interaction.guild, "polls") is None:
            return await interaction.response.send_message(
                "There is no room bound for the `polls` job, so a poll open "
                "to everyone has nowhere to go. An admin can point me at one "
                "with `/setup`.",
                ephemeral=True,
            )
        await interaction.response.send_modal(PollModal())

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
            "Removal is the cooperative's heaviest instrument: a "
            f"{numbers(interaction.guild)['removal_hours']:g}-hour window, "
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


async def finalize_bill(guild, bill, passed, tally_line, decided=None):
    """Common closing: result card, published if passed, seal the notes
    thread, close the ballot, seal the debate."""
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
    floor = floor_for(guild, bill)

    # An open poll is advisory, and the record is for decisions. Numbering
    # one alongside the cooperative's own would put a thing that binds
    # nobody in the same list as the things that do, where the next person
    # to read the record has no way to tell them apart -- and the standing
    # orders promise the opposite in as many words. The result stands in
    # the polls room, where it was asked.
    advisory = audience_of(bill) == EVERYONE
    act_line = ""
    if passed and not advisory:
        act_line = await publish_act(guild, bill, decided)

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

    await seal_chamber(guild, bill)

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
        "The notes and the debate are sealed with the proposal.",
    ]
    outstanding = []

    if audience_of(bill) == EVERYONE:
        # Said whichever way it went, because "the room said yes" and "the
        # room decided" are different sentences and only one of them is
        # true. An advisory poll that quietly read as a decision would be
        # the most useful lie in the building.
        done.append(
            "This was a poll open to the whole server, not a decision. It "
            "is advisory: it binds nobody and is not on the record."
        )
        outstanding.append(
            "If the cooperative wants to act on it, that is a proposal, "
            "and it is a separate vote."
            if passed else
            "Nothing to do. Anyone may ask the room again, reworded, "
            "whenever they like."
        )
        return {
            "bill": bill["no"],
            "title": bill.get("title", ""),
            "ruling": "passed" if passed else "failed",
            "tally": bill.get("tally_line", ""),
            "act": None,
            "advisory": True,
            "done": done,
            "outstanding": outstanding,
        }

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
    floor = floor_for(guild, bill)
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
        required = max(len(eligible) - 2, numbers(guild)["kick_min_yes"])
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
    if st["open_kind"]:
        # An open poll is carried by a majority of the votes cast, and the
        # quorum is the whole of what stops a handful of people deciding
        # for a server. Abstentions count toward turning up and toward
        # nothing else: somebody who came and declined to pick has helped
        # the poll count without being made to have an opinion.
        if st["size"] <= 0:
            # Same protection the roster-counted path has, for the same
            # reason: a cold member cache reads as an empty room, and
            # failing a poll because we cannot see who is in it would be
            # wrong fast. Fall back to the votes actually cast.
            passed = yes > no
            line += " · roster unreadable; counted on the votes cast"
        else:
            bill["threshold"] = {
                "roster": st["size"], "quorum": st["quorum"],
                "audience": EVERYONE,
            }
            met = st["voted"] >= st["quorum"]
            passed = met and yes > no
            line += (
                f" · {st['voted']} of {st['size']} voted, quorum {st['quorum']}"
                + ("" if met else " — not met")
            )
    elif st["size"] > 0:
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
    ends = now_utc() + timedelta(hours=numbers(guild)["floor_hours"])
    bill["ends_at"] = ends.isoformat()
    # A runoff is a fresh vote wearing the same number: it keeps its filing
    # date, so anything measuring how far through the window we are has to
    # measure from here instead.
    bill["round_opened_at"] = now_utc().isoformat()

    floor = floor_for(guild, bill)
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
        await floor.send(
            view=Card([
                f"## Proposal No. {bill['no']}: {bill['title']}: runoff\n"
                f"No option won a majority ({tally_line}). The vote "
                f"reopens with the leading options; regroup around what "
                f"can win."
            ])
        )
        ballot = await floor.send(
            ballot_content(guild, bill), view=MultiBallotView(finalists)
        )
        bill["ballot_message_id"] = ballot.id

    await update_bill(bill)
    log.info(f"runoff opened: proposal no. {bill['no']} ({tally_line})")


@tasks.loop(seconds=60)
async def check_floor():
    guild = home_guild()
    if guild is None:
        return
    # Polls run on the same machinery, so either feature keeps the clock
    # ticking: a vote already on the floor when governance was switched off
    # still deserves to be closed rather than left open forever.
    if not (module_live(guild, "governance") or module_live(guild, "polls")):
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

    Never a poll open to the whole server, whatever else is true of it. The
    nudge exists because silence is a no, and on an open poll it is not;
    what would be left is a bot direct-messaging a whole server about a
    poll nobody asked it about. A quorum that goes unmet is an answer too.
    """
    if audience_of(bill) == EVERYONE:
        return False
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

    What silence costs depends on the vote. A yes/no ballot is carried
    against the whole roster, so not voting is as good as a no. A choice
    ballot is settled among the votes cast, so silence is not a vote
    against anything -- it just hands the answer to whoever did turn up,
    and saying otherwise would be a lie told to get somebody to click.
    An open poll is the same, and is never nudged about at all.
    """
    st = vote_state(guild, bill)
    if st["options"] or st["open_kind"]:
        cost = "and the answer is being chosen without you"
    elif is_blind(bill):
        cost = "and the house is still short of a view"
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
    if not module_live(guild, "governance"):
        return
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


async def act_open_poll(guild, invoker, args):
    """A question put to the whole server, in the asker's name.

    The same door as a proposal and a different room behind it. Nothing
    here decides anything -- an open poll is advisory by construction, and
    the standing orders say a cooperative member may open one alone -- so
    it needs no more permission than filing does.
    """
    title = _clean(args.get("title"), 100)
    what = _clean(args.get("what"), 4000)
    why = _clean(args.get("why"), 4000)
    if not (title and what and why):
        return json.dumps({"error": "a poll needs a title, a question, and a why"})
    options = []
    for raw in args.get("options") or []:
        line = _clean(raw, 100)
        if line and line not in options:
            options.append(line)
    if options and not 2 <= len(options) <= MULTI_MAX:
        return json.dumps(
            {"error": f"a choice ballot needs 2 to {MULTI_MAX} distinct "
                      f"options, or none at all for yes/no"}
        )
    if room(guild, "polls") is None:
        return json.dumps(
            {"error": "no room is bound for the polls job, so a poll open to "
                      "everyone has nowhere to go; an admin can point me at "
                      "one with /setup"}
        )
    bill = await file_bill(guild, invoker, title=title, what=what, why=why,
                           options=options or None, audience=EVERYONE)
    if bill is None:
        return json.dumps({"error": "the polls channel is missing"})
    return json.dumps({"filed": bill["no"], "title": title,
                       "author": bill["author"], "closes_at": bill["ends_at"],
                       "audience": "everyone",
                       "note": "Open to the whole server. Carried by a "
                               "majority of whoever votes, once a quorum of "
                               "the room has. Nobody is chased about it."})


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
        f"delivered privately to the proposer. It is a place in the room; "
        f"it is not a place in the cooperative and not a vote."
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

    floor = floor_for(guild, bill)
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
    for b in load_json(BILLS, []):
        if (b.get("kind") == "kick" and b.get("target_id") == target.id
                and b.get("status") == "on_floor"):
            return json.dumps({"error": f"already up for a vote, No. {b['no']}"})
    why = _clean(args.get("why"), 4000)
    if not why:
        return json.dumps({"error": "a removal without reasons is not a proposal"})

    eligible = [m for m in guild.members
                if in_cooperative(m) and not m.bot and m.id != target.id]
    what = (
        f"{target.display_name} shall be removed from {guild.name}. "
        f"Removal requires yes from all eligible voters but two; the subject "
        f"cannot vote and keeps the whole window to plead. The tally will "
        f"never be published. Eligible voters at filing: {len(eligible)}; the "
        f"threshold is computed at close."
    )
    bill = await file_bill(
        guild, invoker, title=f"Removal of {target.display_name}"[:100],
        what=what, why=why, kind="kick", target_id=target.id,
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


BILL_ACTIONS = {
    "propose_bill": act_propose_bill,
    "open_poll": act_open_poll,
    "propose_member": act_propose_member,
    "propose_removal": act_propose_removal,
    "close_floor": act_close_floor,
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
# split is the same one warden.py uses: the rules are testable on a laptop,
# and only the lookups need a server.

def role_for(guild, key):
    return cooperative_role(guild) if key == "cooperative" else member_role(guild)


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
        f"{mark(coop is not None)} **1 · Roles** — "
        + (f"{coop.mention}" + (f" · {member.mention}" if member else "")
           if coop is not None else
           "no cooperative role yet; Apply makes one"),
        f"{mark(not missing)} **2 · Rooms** — "
        + (f"{len(have_rooms)} bound"
           if not missing else
           "missing " + ", ".join(f"`{r}`" for r in missing)
           + " — Apply makes them"),
        f"{mark(bool(inside))} **3 · The cooperative** — "
        + (f"{len(inside)} " + ("person" if len(inside) == 1 else "people")
           + ("" if you_in else ", and you are not one of them")
           if inside else
           "**empty — nobody can propose, vote or talk to him.** Apply puts "
           "you in"),
        f"✅ **4 · Features** — {on} on, {total - on} off"
        + (f" · waiting on something: "
           f"{', '.join(modules.name(k).lower() for k in stuck)}"
           if stuck else ""),
        f"{mark(has_brain(guild))} **5 · Brain** — "
        + (f"awake through {providers.label(brain.provider_name(guild.id))}"
           if has_brain(guild) else
           "no key, so he keeps records and says nothing"),
        f"{mark(bool(settings.get(guild.id, 'house')))} **6 · This place** — "
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

    @discord.ui.button(label="Start fresh…", style=discord.ButtonStyle.danger, row=2)
    async def fresh(self, interaction, button):
        await open_slate(interaction)

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
            + (f" · {len(settings.voting_overrides(guild.id))} of "
               f"{len(settings.VOTING_RULES)} numbers set here"
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
            placeholder="What Eugene does here — tick what you want",
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
            placeholder=f"{key} — {bindings.ROOMS[key]}",
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
        label="Which channel — name, id, or a link",
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
            placeholder=f"{key} — {bindings.ROLES[key]}",
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
                    " — my role has to sit above the cooperative's."
        await open_roles(
            interaction,
            note + "\n-# This is how the cooperative grows: somebody in it "
                   "hands it over here. `/invite` is a different door — it "
                   "puts a stranger in the server, not on this roll.",
        )


class RevokeSelect(discord.ui.UserSelect):
    """Undoing a typo, not removing a person. An actual removal is
    `/remove`: a vote at the fundamental tier with a sealed tally."""

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
                f"Could not: {e!r} — my role has to sit above {coop.mention}.",
            )
        await open_roles(interaction, f"You are in. {coop.mention} is yours.")


async def open_roles(interaction, note=None):
    guild = interaction.guild
    view = StewardView(interaction.user.id)
    view.add_item(RoleBindSelect("cooperative", 0))
    view.add_item(RoleBindSelect("member", 1))
    view.add_item(GrantSelect(2))
    view.add_item(RevokeSelect(3))
    view.add_item(JoinButton())
    inside = cooperative_members(guild)
    body = [
        "## Who holds a vote",
        "`cooperative` votes. `member` is in the room without one — leave it "
        "unbound if everyone here votes.",
        "",
        f"**In the cooperative: {len(inside)}**"
        + (" — " + ", ".join(m.display_name for m in inside[:20])
           + ("…" if len(inside) > 20 else "")
           if inside else
           " — **nobody. He refuses everyone, including you, until somebody "
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
        await update_health(self.guild)


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
        await update_health(guild)
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
        await update_health(guild)
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
# The clerk keeps half his memory per server and half of it at the top of
# the data directory, from when there was only ever one house. Move the
# daemon to a new server and the second half follows: last house's
# proposals, last house's decisions, last house's numbering, and forty
# notes about people who are not there.
#
# `slate.py` knows what would go; this is the asking. Two guards, because
# there is no undo and the thing on the other side of the button is a
# parliament's entire record: nothing is ticked to begin with, and the
# button does not erase anything -- it opens a box you have to type a word
# into, having read what each scope costs.


class SlateSelect(discord.ui.Select):
    def __init__(self, guild, picked):
        options = []
        for scope in slate.keys():
            spec = slate.SCOPES[scope]
            found = slate.present(guild.id, scope)
            total = sum(len(v) for v in found.values())
            options.append(discord.SelectOption(
                label=spec["name"],
                value=scope,
                description=(spec["blurb"] if total else
                             "nothing written here yet")[:100],
                default=scope in picked,
            ))
        super().__init__(
            placeholder="What to erase — nothing is ticked to begin with",
            min_values=0, max_values=len(options), options=options, row=0,
        )

    async def callback(self, interaction):
        await open_slate(interaction, picked=set(self.values))


class SlateConfirm(discord.ui.Modal, title="This cannot be undone"):
    word = discord.ui.TextInput(
        label="Type ERASE to go ahead",
        style=discord.TextStyle.short,
        max_length=10,
        placeholder="ERASE",
    )

    def __init__(self, guild, picked):
        super().__init__()
        self.guild = guild
        self.picked = picked

    async def on_submit(self, interaction):
        if str(self.word).strip().upper() != "ERASE":
            return await open_slate(
                interaction, picked=self.picked,
                note="Nothing was erased — that was not the word.",
            )
        gone = slate.wipe(self.guild.id, self.picked)
        # The rolling transcript and the heartbeat's buffer are in this
        # process, not on disk, so a wipe that left them would have him
        # quoting a room he had just been told to forget.
        if "memory" in self.picked:
            brain.forget_room()
        log.warning(
            f"{interaction.user.display_name} erased "
            f"{', '.join(self.picked)} in guild {self.guild.id}"
        )
        await health_log(
            self.guild,
            "🧹 " + ", ".join(slate.name(s) for s in self.picked)
            + f" erased by {interaction.user.display_name}.",
        )
        lines = ["## Erased"]
        for scope in slate.keys():
            if scope in gone:
                lines.append(f"- **{slate.name(scope)}** — "
                             + ", ".join(f"`{x}`" for x in gone[scope]))
        empty = [s for s in self.picked if s not in gone]
        if empty:
            lines.append("- Nothing was stored for "
                         + ", ".join(slate.name(s) for s in empty) + ".")
        lines.append("")
        lines.append("-# Nothing in Discord was touched: no channel, no role "
                     "and no message was deleted. He has simply stopped "
                     "knowing about them.")
        await interaction.response.edit_message(
            content="\n".join(lines)[:1990],
            view=StewardView(interaction.user.id),
        )


class SlateEraseButton(discord.ui.Button):
    def __init__(self, picked):
        super().__init__(
            label=f"Erase the {len(picked)} ticked…" if picked
                  else "Tick something first",
            style=discord.ButtonStyle.danger if picked
                  else discord.ButtonStyle.secondary,
            disabled=not picked, row=4,
        )
        self.picked = picked

    async def callback(self, interaction):
        await interaction.response.send_modal(
            SlateConfirm(interaction.guild, self.picked)
        )


async def open_slate(interaction, picked=(), note=None):
    guild = interaction.guild
    picked = {s for s in picked if s in slate.SCOPES}
    view = StewardView(interaction.user.id)
    view.add_item(SlateSelect(guild, picked))
    view.add_item(SlateEraseButton(picked))
    body = [
        "## Start fresh",
        "For pointing him at a different server, or for putting the "
        "numbering back to one. **There is no undo.**",
        "",
        slate.summary(guild.id),
    ]
    if picked:
        body += ["", "**About to erase:**"]
        body += [f"- **{slate.name(s)}** — {slate.SCOPES[s]['costs']}"
                 for s in slate.keys() if s in picked]
    if slate.stranded(guild.id):
        body.insert(2, f"⚠️ This data directory was last used by server "
                       f"`{slate.owner()}`, not this one. What is written "
                       f"here is that server's.")
    if note:
        body += ["", note]
    await interaction.response.edit_message(
        content="\n".join(body)[:1990], view=view
    )


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

# How each number reads to somebody who has to decide whether to change it.
VOTING_BLURBS = {
    "floor_hours": "how long an ordinary vote stays open if nothing settles it",
    "removal_hours": "the same, for a removal",
    "fundamental_share": "the share of the roster a removal or a rule change needs",
    "public_quorum_share": "the share of the server that must vote for an open poll to count",
    "kick_min_yes": "the fewest yes votes a removal can ever pass on",
    "away_days": "a quiet spell this long takes you out of the count",
}


def voting_lines(guild):
    """Every number, what it means, and whether it is theirs or his."""
    held = numbers(guild)
    chosen = settings.voting_overrides(guild.id)
    rows = []
    for name, blurb in VOTING_BLURBS.items():
        value = held[name]
        shown = f"{value:g}" if isinstance(value, float) else str(value)
        # Not `-#` subtext: Discord only honours that at the start of a
        # line, and this is the end of one.
        mark = "" if name in chosen else " *(his default)*"
        rows.append(f"- `{name}` **{shown}** — {blurb}{mark}")
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
        tail = (
            "Votes already on the floor keep the window they were filed with; "
            "thresholds are worked out fresh, so those move now."
            if number in ("floor_hours", "removal_hours")
            else "It applies to every vote from this moment, including the "
                 "ones already open."
        )
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


async def open_numbers(interaction, note=None):
    guild = interaction.guild
    view = StewardView(interaction.user.id)
    view.add_item(NumberSelect(guild))
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

    def __init__(self, guild):
        super().__init__()
        current = settings.get(guild.id, "house")
        if current:
            self.description.default = current

    async def on_submit(self, interaction):
        text = " ".join(str(self.description).split())[:300]
        settings.put(interaction.guild.id, house=text or None)
        note = (
            f"He now knows **{interaction.guild.name}** as *{text}*."
            if text else
            f"Reset. He will describe this place as "
            f"*{brain.DEFAULT_HOUSE}* — true of most servers, specific to none."
        )
        await show_panel(interaction, note)


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
        f"{'✅' if coop else '➕'} `Cooperative` — holds a vote"
        + (f" (you have {coop.mention})" if coop else ""),
        f"{'✅' if member else '➕'} `Member` — in the room, no vote",
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
            + (f" — {modules.CATEGORIES[category]}"
               if category in modules.CATEGORIES else "")
        )
        for job in rooms:
            spec = modules.ROOM_PLAN[job]
            bound = bindings.channel(guild, job)
            found = None if bound else find_channel(guild, job)
            if bound:
                mark, shown, tail = "✅", bound.mention, "already bound"
            elif found:
                mark, shown, tail = "🔗", found.mention, "adopted as it is"
            else:
                mark, shown, tail = "➕", f"#{spec['name']}", "created"
            wanted = modules.wanted_by(guild.id, job)
            lines.append(
                f"　{mark} {shown} — {tail}; "
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
        "-# Every line above is additive. A channel that already exists is "
        "adopted exactly as it stands — never renamed, re-topiced, moved, "
        "re-permissioned or deleted. Nothing else in your server is touched."
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


async def make_missing_rooms(guild, categories=None):
    """Create and bind a channel for every job an enabled feature wants and
    has none. Strictly additive: it never renames, moves, re-topics,
    re-permissions, reorders or deletes anything that already exists --
    including channels it finds by name, which are adopted exactly as they
    are. Nothing outside the plan is touched at all.

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
        # By the job's name, not by the bare word. The layout used to call
        # this room "🗳️・votes" and this command looked for "votes", found
        # nothing, and created a second one loose at the top of the sidebar
        # -- so a server built from the terminal and then set up from inside
        # Discord ended up with two of every governance room, Eugene bound
        # to the empty one. `find_channel` is the same lookup the rest of
        # the bot uses to find a room nobody has bound, which is the point:
        # what gets bound here and what gets found without a binding must be
        # the same channel.
        existing = find_channel(guild, key)
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
        existing = find_channel(guild, key)
        if existing is None:
            skipped.append(f"`{key}` — nothing here to use, so nothing made")
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
            content="I cannot do this yet — I am missing: "
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

        # The whole point: somebody ends up inside.
        you = interaction.user
        if coop in you.roles:
            say("you were already in the cooperative")
        else:
            try:
                await you.add_roles(coop, reason="/setup: the first member")
                say(f"gave you {coop.mention}")
            except discord.HTTPException as e:
                say(f"could not give you {coop.mention}: {e!r} — "
                    "check my role sits above it in the list")

        wanted = [c for c, _rooms
                  in modules.structure(guild.id, only_buildable=True)]
        categories = await ensure_categories(guild, wanted, say)
        made, bound, skipped = await make_missing_rooms(guild, categories)
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
        left.append("**Brain** — a key, so he can talk. Without one he keeps "
                    "records and holds the door, and says nothing.")
    left.append(
        "**Roles & votes** — hand the cooperative to anyone else who should "
        "have one. That is the only way onto that roll; `/invite` is the "
        "server's door and a vote, and it hands out a link, not a ballot."
    )
    if not settings.get(guild.id, "house"):
        left.append(
            "**What this place is** — one line on what this server is for. He "
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
    await update_health(guild)


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
    name="poll", description="Ask the whole server a question"
)
@app_commands.guild_only()
async def slash_poll(interaction: discord.Interaction):
    if await refuse_unless(interaction, "polls"):
        return
    if not keyed_in(interaction):
        return await refuse(interaction, NOT_INSIDE)
    await interaction.response.send_modal(PollModal())


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
        "Removal is the cooperative's heaviest instrument: a 72-hour window, "
        "and it passes only if all eligible voters but two say yes.",
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
    if report.get("tally") and report["tally"] != "sealed":
        lines.append(report["tally"])
    if report.get("outstanding"):
        lines += ["", "Still wanted:"] + [f"- {item}" for item in report["outstanding"]]
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(name="bills", description="What is open for a vote right now")
@app_commands.guild_only()
async def slash_bills(interaction: discord.Interaction):
    """What is open, to whoever is asking.

    Not cooperative-only any more, because not everything open is the
    cooperative's. Somebody in the room who is not in the cooperative gets
    the polls they can actually vote in and nothing else -- listing the
    proposals at them would only be a list of things they cannot do.
    """
    member = interaction.user
    inside = keyed_in(interaction)
    if not inside and not in_room(member):
        return await refuse(interaction, NOT_INSIDE)
    # Two features can put something on this list, and it is worth reading
    # while either one of them runs.
    live = [k for k in ("governance", "polls")
            if module_live(interaction.guild, k)]
    if not live:
        return await refuse(interaction, module_note(interaction.guild,
                                                     "governance"))

    open_bills = sorted(
        (b for b in load_json(BILLS, [])
         if b.get("status") == "on_floor"
         and (inside or audience_of(b) == EVERYONE)),
        key=lambda b: b["no"],
    )
    if not open_bills:
        return await interaction.response.send_message(
            "Nothing is open. The floor is yours." if inside
            else "No polls are open just now.",
            ephemeral=True,
        )
    lines = ["**Open for a vote**" if inside else "**Open to the room**"]
    # A title can run to a hundred characters, so a busy floor is trimmed
    # rather than sent and refused by Discord for length.
    for bill in open_bills[:10]:
        ends = int(datetime.fromisoformat(bill["ends_at"]).timestamp())
        mark = ""
        if audience_of(bill) == EVERYONE:
            mark = "📣 " if inside else ""
        elif is_priority(bill):
            mark = "⚡ "
        where = floor_for(interaction.guild, bill)
        lines.append(
            f"{mark}**No. {bill['no']}: {bill['title']}** — "
            f"{bill.get('author', 'someone')}, closes <t:{ends}:R>"
            + (f" · {where.mention}" if where else "")
        )
    rest = len(open_bills) - 10
    if rest > 0:
        lines.append(f"-# And {rest} more.")
    if inside:
        lines.append("-# 📣 is open to the whole server; ⚡ is one you will "
                     "be chased about.")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="house", description="What Eugene is running for this server")
@app_commands.describe(group="One group of settings, in full: automod, warnings, log…")
@app_commands.guild_only()
async def slash_house(interaction: discord.Interaction, group: str = None):
    """The settings, read-only, without spending a thought on it.

    Everything here is changed by asking him instead, and that is the point
    of the command: a house whose brain has no key, or whose bill has run
    out for the month, can still see what its own filters are set to. A
    switch you cannot read is a switch nobody trusts.
    """
    if not keyed_in(interaction):
        return await refuse(interaction, "That one is for people who are in.")
    guild = interaction.guild
    if group and group.lower() not in warden.GROUPS:
        return await refuse(
            interaction, f"Groups are: {', '.join(warden.GROUPS)}."
        )
    if not group:
        chosen = len(warden.overrides(guild.id))
        tail = ("\n-# Nothing has been changed from the defaults yet — just "
                "tell me what you want." if not chosen else
                f"\n-# {chosen} of these are yours; the rest are the defaults.")
        return await interaction.response.send_message(
            f"## What {guild.name} has switched on\n"
            + module_summary(guild)
            + "\n\n" + powers.summary(guild) + tail,
            ephemeral=True,
        )
    now = warden.config(guild.id)
    # A setting group belongs to a feature, and a group whose feature is off
    # is a page of numbers nothing reads. Say so at the top rather than
    # letting somebody tune a filter that will never run.
    owner = modules.of_setting(group.lower())
    header = f"**{group.lower()}**"
    if owner and not modules.enabled(guild.id, owner):
        header += (f"\n-# {modules.name(owner)} is switched off, so none of "
                   f"these are read. `/setup` switches it back on.")
    lines = [header]
    for key, spec in warden.describe(group.lower()).items():
        value = now.get(key)
        if spec["type"] == "channel" and value:
            got = interaction.guild.get_channel(value)
            value = f"#{got.name}" if got else f"{value} (gone)"
        elif spec["type"] == "role" and value:
            got = interaction.guild.get_role(value)
            value = got.name if got else f"{value} (gone)"
        lines.append(f"`{key}` = **{value}**\n-# {spec['help']}")
    await interaction.response.send_message("\n".join(lines)[:1990], ephemeral=True)


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
    "haiku": "cheap and quick — the one he came with",
    "sonnet": "the middle rung, three times haiku",
    "opus": "the good one, five times haiku",
}


def _claude_tier_choices():
    """The rungs Claude names, as Discord choices. Built from providers.py
    rather than typed twice, so a fourth tier is one line over there."""
    return [
        app_commands.Choice(
            name=f"{tier} — {CLAUDE_TIER_BLURB.get(tier, model)}", value=tier
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
            f"- **{name}** — `{model}`" + (" ← here" if model == now else "")
            for name, model in tiers.items()
        )
        return await refuse(
            interaction,
            f"Claude is on `{now}`"
            + (f" ({standing})." if standing else
               " — not one of the named rungs, so it stays untouched "
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
    await update_health(guild)


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
        {**BILL_ACTIONS, **DUTY_ACTIONS,
         **powers.ACTIONS_TABLE},
        in_cooperative=in_cooperative, numbers=numbers,
    )
    # The officer's hands. in_cooperative goes in twice on purpose: the
    # harness uses it to decide who may reach the elevated tools at all,
    # and powers.py uses it to decide who those tools may be used on.
    powers.configure(bot, in_cooperative, health_log)
    # And the desk the heavy half of those hands now waits at. It is
    # given the administrator check rather than the roll: the whole point
    # of a sign-off is that it comes from somewhere other than the
    # cooperative, which is already the thing that asked.
    sanction.configure(bot, is_admin)
    settings.configure(DATA)
    # server_config.yaml's window becomes the default every house starts
    # from, and stops being the rule the moment one sets its own.
    settings.configure_voting(floor_hours=CONFIG_FLOOR_HOURS)
    slate.configure(DATA)
    roster.configure(DATA)
    duties.configure(DATA)
    # Upgrade in place: a host that still carries a key in its environment
    # hands it to the server it serves, once, and is never read again.
    adopted = settings.adopt_env_keys(GUILD_ID)
    if adopted:
        log.info(f"adopted host {', '.join(adopted)} key(s) into guild {GUILD_ID}")
    brain.configure(
        bot, HERE, DATA, in_cooperative, health_log, chunk_text, resolve_guild,
        numbers=numbers,
    )
    if not intents.message_content:
        log.warning(
            "running without the Message Content intent: Eugene cannot "
            "hear anything, whatever keys a server sets — and the filters "
            "read an empty message, so automod is off in fact whatever the "
            "settings say"
        )
    bot.add_view(SubmitBillView())
    bot.add_view(BallotView())
    bot.add_view(MemberBallotView())
    # Dummy labels: this instance exists only to route clerk:opt_* presses
    # after a restart. Without it a deploy mid-vote leaves every choice
    # ballot on the floor with dead buttons and no way to say so.
    bot.add_view(MultiBallotView())
    bot.add_view(NotesView())
    # Same reason as the ballots: a deploy while a ban is waiting to be
    # signed must not leave the administrator holding a dead card.
    bot.add_view(sanction.SignOffView())
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)


async def ensure_furniture(guild, restamp=False):
    """Post any missing furniture. Runs at boot (restamp=True, so
    deploys refresh buttons and wording) and periodically (verify only)."""
    if module_live(guild, "governance"):
        await ensure_button_message(
            room(guild, "proposals"),
            "bill_message_id",
            "*Say what should change, and why. Eugene files it and the "
            "cooperative votes. Authorship is public; rules do not have "
            "anonymous authors. A poll is the other thing — the same question "
            "put to the whole server, deciding nothing.*",
            SubmitBillView(),
            restamp=restamp,
        )
    if module_live(guild, "health"):
        await close_health_room(guild)
        await update_health(guild)


async def close_health_room(guild):
    """Shut the health room to everyone but administrators, once.

    New servers get this from the room plan, which `/setup` reads. Servers
    that already have the room do not: everything `/setup` does is additive
    and it never re-permissions a channel, which is the promise that lets
    people run it without reading the code first. So the one room that
    changed audience is changed here instead, deliberately and by name.

    Once, and tracked, because the alternative is a loop that argues. An
    administrator who opens this room back up to the cooperative has
    decided something, and a bot that quietly re-shuts it every five
    minutes is overriding a person who outranks it on exactly the question
    the room is about.
    """
    state = load_json(STATE, {})
    if state.get("health_closed"):
        return
    channel = health_channel(guild)
    if channel is None:
        return
    try:
        await channel.edit(
            overwrites=admin_only_overwrites(guild),
            reason="bot-health is administrators only",
        )
    except discord.HTTPException as e:
        log.warning(f"could not close the health room: {e!r}")
        return
    state["health_closed"] = True
    save_json(STATE, state)
    log.info(f"#{channel.name} is now administrators only")
    await health_log(
        guild,
        "🔒 This room is administrators only now. The opt-in `nerd` role "
        "that used to open it is gone; nothing here was ever anything but "
        "operational detail. The `nerd` role itself is left where it is — "
        "delete it whenever you like, it does nothing.",
    )


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
        # A sign-off card nobody got to should stop looking live. Five
        # minutes of a lapsed request still showing its buttons is
        # harmless; a day of it is a card somebody presses expecting a ban.
        try:
            await sanction.sweep(guild)
        except Exception as e:
            log.error(f"the sign-off sweep failed: {e!r}")
            await health_log(guild, f"⚠️ Furniture check failed: `{e!r}`")


@bot.event
async def on_ready():
    log.info(f"on duty as {bot.user} (commit {COMMIT})")
    guild = home_guild()
    if guild:
        # Whose history is in this directory. A wrong GUILD_ID is a typo
        # somebody fixes in a minute; a record erased on their behalf
        # because of one is not recoverable, so this says it and stops.
        if slate.stranded(GUILD_ID):
            log.warning(
                f"data directory belongs to guild {slate.owner()}, not "
                f"{GUILD_ID}: this server is reading another one's record. "
                f"`/setup` → Start fresh clears it."
            )
            await health_log(
                guild,
                f"⚠️ The record on this disk was written by server "
                f"`{slate.owner()}`, not this one, so the proposals, "
                f"decisions and numbering here are theirs. **`/setup` → "
                f"Start fresh** clears it. Nothing is erased on its own.",
            )
        else:
            slate.claim(GUILD_ID)
        await ensure_furniture(guild, restamp=not getattr(bot, "_boot_announced", False))
        if not getattr(bot, "_boot_announced", False):
            bot._boot_announced = True
            await repaint_open_ballots(guild)
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
    # The filters first, and the counter with them. A message that has just
    # been deleted for breaking a house rule is not also a message to answer:
    # replying to something nobody can see reads as talking to yourself.
    try:
        if message.guild is not None and serves(message.guild):
            if await powers.on_message(message):
                return
    except Exception as e:
        log.error(f"automod error: {e!r}")
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


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot or not serves(member.guild):
        return
    # Two unrelated things, on two switches. The hello is Arrivals and goes
    # in a room somebody chose; writing the arrival down is the audit log's
    # and goes wherever that writes. They were one call behind one gate,
    # which meant a server that did not want a public greeting also lost
    # the private record of who came and went.
    try:
        if module_live(member.guild, "welcome"):
            await powers.on_join(member)
    except Exception as e:
        log.error(f"welcome error: {e!r}")
    try:
        if module_live(member.guild, "log"):
            await powers.on_join_logged(member)
    except Exception as e:
        log.error(f"join log error: {e!r}")


@bot.event
async def on_member_remove(member: discord.Member):
    if member.bot or not serves(member.guild):
        return
    try:
        if module_live(member.guild, "welcome"):
            await powers.on_leave(member)
    except Exception as e:
        log.error(f"goodbye error: {e!r}")
    try:
        if module_live(member.guild, "log"):
            await powers.on_leave_logged(member)
    except Exception as e:
        log.error(f"leave log error: {e!r}")


@bot.event
async def on_message_delete(message: discord.Message):
    if message.guild is not None and serves(message.guild):
        try:
            await powers.on_delete(message)
        except Exception as e:
            log.error(f"delete log error: {e!r}")


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.guild is not None and serves(after.guild):
        try:
            await powers.on_edit(before, after)
        except Exception as e:
            log.error(f"edit log error: {e!r}")


if __name__ == "__main__":
    logging.getLogger("discord").setLevel(logging.WARNING)
    bot.run(TOKEN, log_level=logging.INFO)
