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
import people
import powers
import providers
import pulse
import roster
import sanction
import settings
import slate
import survey
import toolbox
import warden

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

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
ROLES = DATA / "roles.json"  # custom role registry: {role_id: {creator_id}}


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
NERD = "nerd"    # opt-in subscription to the bot-health channel, not a rank

# Said to somebody who is not in the cooperative. It names the way in on
# purpose: a refusal that only says no leaves a new arrival stuck, which is
# exactly how the first install went -- the person who installed Eugene was
# outside, and nothing anywhere told them how to get inside.
NOT_INSIDE = (
    "Only the cooperative files proposals here. Anyone already inside can "
    "put you up with `/invite`; whoever runs the place can hand it over "
    "with `/setup`."
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
        f"Vote window: {numbers(guild)['floor_hours']:g}h",
        brain.status_line(guild.id),
        # Only when there is something waiting. A card in the log room is
        # easy to scroll past, and a sign-off nobody notices is a
        # moderation action that silently never happened -- which is the
        # one way this whole arrangement fails quietly.
        *([f"⏳ {waiting}"] if (waiting := sanction.summary(guild.id)) else []),
        f"-# Updated <t:{int(now_utc().timestamp())}:R>. "
        f"Opt out with Nerd mode in the roles channel.",
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

def chamber_overwrites(guild):
    return {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        cooperative_role(guild): discord.PermissionOverwrite(view_channel=True),
    }


def open_chamber_overwrites(guild):
    """The debate room for a poll put to the whole server. Left alone --
    whatever the server's own default is, that is who the poll is open to,
    and inheriting it is the only way the room the ballot counts and the
    room the ballot admits stay the same set of people."""
    return {}


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


async def open_chamber(guild, number, title, audience=COOPERATIVE_ONLY):
    ow = (open_chamber_overwrites(guild) if audience == EVERYONE
          else chamber_overwrites(guild))
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
    chamber = guild.get_channel(bill.get("chamber_text_id")) if guild else None
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
    room it is posted in, who the chamber is open to, what carries it. It
    defaults to the closed kind, because a caller that forgets to say has
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

    category, text, voice = await open_chamber(
        guild, number, title, audience=audience
    )

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
    chamber = interaction.guild.get_channel(bill["chamber_text_id"])
    await interaction.followup.send(
        f"Filed. Proposal No. {bill['no']} is open: {floor.mention}, "
        f"debate in {chamber.mention}.",
        ephemeral=True,
    )
    return bill


# ---------- submitting bills ----------

class BillModal(discord.ui.Modal, title="Make a proposal"):
    def __init__(self, priority=False, prefill=None, from_draft=None):
        super().__init__()
        self.priority = bool(priority)
        self.from_draft = from_draft
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
        if bill is not None and self.from_draft is not None:
            pulse.drop_draft(self.from_draft)
            try:
                message = await interaction.channel.fetch_message(self.from_draft)
                await message.edit(
                    content=f"-# Filed by {interaction.user.display_name} as "
                            f"Proposal No. {bill['no']}.",
                    view=None,
                )
            except discord.HTTPException:
                pass


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
# How often the heartbeat wakes to look. Looking is free; see pulse.py.
PULSE_MINUTES = 20


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


# ---------- the heartbeat ----------
# The other proactive loop, and the only one that spends. duties.py is free
# by construction and stays that way; everything below is fenced instead.
#
# The order is the whole design: a timer wakes it, plain arithmetic decides
# whether anything has happened, and only then is the model asked. A quiet
# server costs nothing at all, and a busy one costs at most MAX_PER_DAY
# thoughts however wrong the gate turns out to be.


class DraftView(discord.ui.View):
    """A proposal he wrote, with a button that files it in somebody else's
    name.

    This is the whole of his volition and the limit of it. He is not an
    author here and never becomes one: whoever presses the button is the
    author, wanted it, and can throw it away first. If nobody presses,
    nothing was proposed -- which is the correct outcome for an idea only
    the bot had.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="File this", emoji="🖋️",
        style=discord.ButtonStyle.primary, custom_id="clerk:draft_file",
    )
    async def file(self, interaction: discord.Interaction, button):
        if not in_cooperative(interaction.user):
            return await interaction.response.send_message(
                NOT_INSIDE, ephemeral=True
            )
        held = pulse.draft(interaction.message.id)
        if held is None:
            return await interaction.response.send_message(
                "That draft has gone. Anyone can write it out with "
                "`/propose`, which is all this button ever did.",
                ephemeral=True,
            )
        await interaction.response.send_modal(
            BillModal(prefill=held, from_draft=interaction.message.id)
        )

    @discord.ui.button(
        label="Not interested", emoji="🗑️",
        style=discord.ButtonStyle.secondary, custom_id="clerk:draft_drop",
    )
    async def drop(self, interaction: discord.Interaction, button):
        if not in_cooperative(interaction.user):
            return await interaction.response.send_message(
                NOT_INSIDE, ephemeral=True
            )
        held = pulse.draft(interaction.message.id)
        pulse.drop_draft(interaction.message.id)
        if held:
            # Already marked raised when it was posted; this only makes the
            # refusal explicit in the log. He does not re-raise either way.
            log.info(f"draft declined by {interaction.user.display_name}: "
                     f"{held.get('topic')!r}")
        try:
            await interaction.message.edit(
                content="-# A draft nobody wanted. Cleared.", view=None
            )
        except discord.HTTPException:
            pass
        await interaction.response.send_message(
            "Cleared, and I will not raise it again.", ephemeral=True
        )


def governance_digest(guild):
    """Where things stand, in the fewest words that are still true. Free:
    every figure comes off files already on disk."""
    bills = load_json(BILLS, [])
    lines = []
    for bill in bills:
        if bill.get("status") != "on_floor":
            continue
        st = vote_state(guild, bill)
        kind = "poll (everyone)" if st["open_kind"] else "proposal"
        lines.append(
            f"- No. {bill['no']} {bill['title']!r} ({kind}), "
            f"{st['voted']} of {st['size']} voted, closes {bill.get('ends_at')}"
        )
    items = duties.outstanding(bills, closing_report)
    if items:
        lines.append(
            f"- {len(items)} decision(s) passed and not carried out: "
            + "; ".join(str(i.get("title", "")) for i in items[:4])
        )
    return "\n".join(lines)


def pulse_material(guild):
    """Everything the gate needs, and not one token spent gathering it."""
    said, speakers = brain.fresh_counts()
    bills = load_json(BILLS, [])
    open_bills = []
    latest = None
    for bill in bills:
        submitted = bill.get("submitted_at")
        if submitted and (latest is None or submitted > latest):
            latest = submitted
        if bill.get("status") != "on_floor":
            continue
        st = vote_state(guild, bill)
        open_bills.append({
            "no": bill["no"], "ends_at": bill.get("ends_at"),
            "waiting": st["waiting"],
        })
    items = duties.outstanding(bills, closing_report)
    return {
        "messages": said,
        "speakers": len(speakers),
        "speaker_ids": sorted(speakers),
        "open_bills": open_bills,
        "outstanding": len(items),
        "outstanding_stale": bool(items) and not duties.chase_due(),
        "floor_idle_since": latest,
    }


async def pulse_speak(guild, answer):
    """Do whatever the one thought decided. Returns what was said, for the
    log, or None."""
    kind = (answer.get("say") or "nothing").strip()
    topic = answer.get("topic") or ""
    text = " ".join((answer.get("text") or "").split())

    if kind == "draft":
        draft = answer.get("draft") or {}
        title = _clean(draft.get("title"), 100)
        what = _clean(draft.get("what"), 4000)
        why = _clean(draft.get("why"), 4000)
        room_ = room(guild, "proposals")
        if not (title and what and why) or room_ is None:
            return None
        body = (
            f"### A draft, if anyone wants it\n{text}\n\n"
            f"**{title}**\n{what}\n\n*Why:* {why}\n"
            f"-# I did not propose this and I have no vote on it. Whoever "
            f"presses the button is the author, and can rewrite every word "
            f"of it first."
        )
        message = await room_.send(body, view=DraftView())
        pulse.keep_draft(message.id, {"title": title, "what": what, "why": why},
                         topic)
        pulse.mark_raised(topic)
        return f"draft: {title!r}"

    if kind == "remark" and text:
        # Wherever he is allowed to talk, and nowhere else. A server that
        # kept him to one room did not mean "except when he starts it".
        bound = brain.chat_room_id(guild.id)
        where = guild.get_channel(bound) if bound else room(guild, "proposals")
        if where is None:
            return None
        await where.send(text[:1800], allowed_mentions=discord.AllowedMentions.none())
        pulse.mark_raised(topic)
        return f"remark: {text[:60]!r}"

    return None


def pulse_learn(guild, answer):
    """File what he picked up about people. Names are resolved against the
    server, so a note about somebody who does not exist is dropped rather
    than filed under a name he invented.

    A house that has switched memory off is one where he thinks about the
    room and files none of it. Checked here rather than in the prompt: what
    a house has switched off is not a matter of persuasion. The switch and
    not `module_live`, because there is no getting an answer out of the
    model without a key in the first place, and re-asking would only be a
    second way for the same fact to be wrong.
    """
    if not modules.enabled(guild.id, "memory"):
        return 0
    filed = 0
    for item in (answer.get("people") or [])[:3]:
        if not isinstance(item, dict):
            continue
        who = (item.get("who") or "").strip()
        text = (item.get("text") or "").strip()
        if not who or not text:
            continue
        member = discord.utils.find(
            lambda m: not m.bot and who.lower() in (
                m.display_name.lower(), getattr(m, "name", "").lower()
            ),
            guild.members,
        )
        if member is None:
            log.debug(f"pulse note about unknown {who!r} dropped")
            continue
        why_not = people.note(member.id, member.display_name, text)
        if why_not is None:
            filed += 1
        else:
            log.debug(f"note about {who!r} not filed: {why_not}")
    return filed


@tasks.loop(minutes=PULSE_MINUTES)
async def pulse_loop():
    """Wake, look for free, and think only if there is cause."""
    guild = home_guild()
    if guild is None or not brain.enabled(guild.id):
        return
    if not module_live(guild, "pulse"):
        return
    if not pulse.due():
        return
    if not pulse.under_cap():
        pulse.record_look()
        return
    if not pulse.within_budget(brain.spend_usd(guild.id),
                               settings.budget_usd(guild.id)):
        pulse.record_look()
        return

    material = pulse_material(guild)
    reason = pulse.gate(material)
    if reason is None:
        pulse.record_look()
        return

    try:
        chat = brain.drain_fresh()
        answer, cost = await brain.pulse_think(
            guild, reason, governance_digest(guild), chat,
            people.digest(material["speaker_ids"]),
        )
    except Exception as e:
        log.error(f"pulse think failed: {e!r}")
        pulse.record_thought()
        return
    pulse.record_thought()
    if not answer:
        return

    learned = pulse_learn(guild, answer)
    topic = answer.get("topic") or ""
    if pulse.raised_recently(topic):
        log.info(f"pulse: {topic!r} raised recently, holding it "
                 f"(${cost:.4f}, learned {learned})")
        return
    try:
        spoke = await pulse_speak(guild, answer)
    except Exception as e:
        log.error(f"pulse speak failed: {e!r}")
        return
    log.info(
        f"pulse [{reason}] ${cost:.4f} learned={learned} "
        f"{spoke or 'said nothing'}"
    )


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


# ---------- custom roles (purely aesthetic) ----------

def role_registry():
    return load_json(ROLES, {})


def created_by(user_id, guild=None):
    """The roles this person made. Given a guild, only the ones that still
    exist.

    The registry is only pruned by our own delete path, so a role removed
    in Discord's own role settings left its entry behind -- and at a cap of
    one that entry blocked its maker from ever making another, with a
    refusal naming a role they could no longer see. A cap should count what
    is there.
    """
    mine = [rid for rid, meta in role_registry().items()
            if meta["creator_id"] == user_id]
    if guild is None:
        return mine
    return [rid for rid in mine if guild.get_role(int(rid)) is not None]


def role_caps(guild=None):
    """The colour-role limits this house runs on.

    The one corner where a caller may genuinely not have a guild to hand:
    the role registry is keyed by role id and knows nothing else, so the
    house he keeps stands in. When that is unknown too -- before the
    gateway is up -- the defaults are what he came with, which is the right
    answer for a bot that is not serving anybody yet.
    """
    return numbers(guild or home_guild())


def at_create_cap(user_id, guild=None):
    """The one reading of the creation cap. The cap blocks new roles only;
    anyone who made several before it came down keeps them."""
    return len(created_by(user_id, guild)) >= role_caps(guild)["role_create_max"]


def role_cap_line(subject="You", guild=None):
    """The refusal when someone is at that cap, said the same way by the
    modal, the panel and Eugene's own hands -- and still reading properly
    if the cap ever moves off one."""
    cap = role_caps(guild)["role_create_max"]
    if cap == 0:
        return f"{subject} cannot make roles here; this house has the cap at nought."
    if cap == 1:
        return f"{subject} already made a role. It has to go before another can exist."
    return f"{subject} already made {cap} roles. One has to go to make room."


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


# Colours by their names, because hex is the part most people cannot write
# and should never have had to. This is the CSS list: not because anything
# here is a web page, but because it is the naming everybody's intuition has
# already been trained on, and a partial list is worse than none -- there is
# no explaining why "teal" works and "turquoise" does not.
#
# Black is the one lie. Discord reads a role colour of 0x000000 as "no
# colour set" and falls back to grey, so somebody asking for black gets the
# nearest black that Discord will actually honour.
COLOUR_NAMES = {
    "aliceblue": 0xF0F8FF, "antiquewhite": 0xFAEBD7, "aqua": 0x00FFFF,
    "aquamarine": 0x7FFFD4, "azure": 0xF0FFFF, "beige": 0xF5F5DC,
    "bisque": 0xFFE4C4, "black": 0x010101, "blanchedalmond": 0xFFEBCD,
    "blue": 0x0000FF, "blueviolet": 0x8A2BE2, "brown": 0xA52A2A,
    "burlywood": 0xDEB887, "cadetblue": 0x5F9EA0, "chartreuse": 0x7FFF00,
    "chocolate": 0xD2691E, "coral": 0xFF7F50, "cornflowerblue": 0x6495ED,
    "cornsilk": 0xFFF8DC, "crimson": 0xDC143C, "cyan": 0x00FFFF,
    "darkblue": 0x00008B, "darkcyan": 0x008B8B, "darkgoldenrod": 0xB8860B,
    "darkgray": 0xA9A9A9, "darkgreen": 0x006400, "darkgrey": 0xA9A9A9,
    "darkkhaki": 0xBDB76B, "darkmagenta": 0x8B008B,
    "darkolivegreen": 0x556B2F, "darkorange": 0xFF8C00,
    "darkorchid": 0x9932CC, "darkred": 0x8B0000, "darksalmon": 0xE9967A,
    "darkseagreen": 0x8FBC8F, "darkslateblue": 0x483D8B,
    "darkslategray": 0x2F4F4F, "darkslategrey": 0x2F4F4F,
    "darkturquoise": 0x00CED1, "darkviolet": 0x9400D3, "deeppink": 0xFF1493,
    "deepskyblue": 0x00BFFF, "dimgray": 0x696969, "dimgrey": 0x696969,
    "dodgerblue": 0x1E90FF, "firebrick": 0xB22222, "floralwhite": 0xFFFAF0,
    "forestgreen": 0x228B22, "fuchsia": 0xFF00FF, "gainsboro": 0xDCDCDC,
    "ghostwhite": 0xF8F8FF, "gold": 0xFFD700, "goldenrod": 0xDAA520,
    "gray": 0x808080, "green": 0x008000, "greenyellow": 0xADFF2F,
    "grey": 0x808080, "honeydew": 0xF0FFF0, "hotpink": 0xFF69B4,
    "indianred": 0xCD5C5C, "indigo": 0x4B0082, "ivory": 0xFFFFF0,
    "khaki": 0xF0E68C, "lavender": 0xE6E6FA, "lavenderblush": 0xFFF0F5,
    "lawngreen": 0x7CFC00, "lemonchiffon": 0xFFFACD, "lightblue": 0xADD8E6,
    "lightcoral": 0xF08080, "lightcyan": 0xE0FFFF,
    "lightgoldenrodyellow": 0xFAFAD2, "lightgray": 0xD3D3D3,
    "lightgreen": 0x90EE90, "lightgrey": 0xD3D3D3, "lightpink": 0xFFB6C1,
    "lightsalmon": 0xFFA07A, "lightseagreen": 0x20B2AA,
    "lightskyblue": 0x87CEFA, "lightslategray": 0x778899,
    "lightslategrey": 0x778899, "lightsteelblue": 0xB0C4DE,
    "lightyellow": 0xFFFFE0, "lime": 0x00FF00, "limegreen": 0x32CD32,
    "linen": 0xFAF0E6, "magenta": 0xFF00FF, "maroon": 0x800000,
    "mediumaquamarine": 0x66CDAA, "mediumblue": 0x0000CD,
    "mediumorchid": 0xBA55D3, "mediumpurple": 0x9370DB,
    "mediumseagreen": 0x3CB371, "mediumslateblue": 0x7B68EE,
    "mediumspringgreen": 0x00FA9A, "mediumturquoise": 0x48D1CC,
    "mediumvioletred": 0xC71585, "midnightblue": 0x191970,
    "mintcream": 0xF5FFFA, "mistyrose": 0xFFE4E1, "moccasin": 0xFFE4B5,
    "navajowhite": 0xFFDEAD, "navy": 0x000080, "oldlace": 0xFDF5E6,
    "olive": 0x808000, "olivedrab": 0x6B8E23, "orange": 0xFFA500,
    "orangered": 0xFF4500, "orchid": 0xDA70D6, "palegoldenrod": 0xEEE8AA,
    "palegreen": 0x98FB98, "paleturquoise": 0xAFEEEE,
    "palevioletred": 0xDB7093, "papayawhip": 0xFFEFD5, "peachpuff": 0xFFDAB9,
    "peru": 0xCD853F, "pink": 0xFFC0CB, "plum": 0xDDA0DD,
    "powderblue": 0xB0E0E6, "purple": 0x800080, "rebeccapurple": 0x663399,
    "red": 0xFF0000, "rosybrown": 0xBC8F8F, "royalblue": 0x4169E1,
    "saddlebrown": 0x8B4513, "salmon": 0xFA8072, "sandybrown": 0xF4A460,
    "seagreen": 0x2E8B57, "seashell": 0xFFF5EE, "sienna": 0xA0522D,
    "silver": 0xC0C0C0, "skyblue": 0x87CEEB, "slateblue": 0x6A5ACD,
    "slategray": 0x708090, "slategrey": 0x708090, "snow": 0xFFFAFA,
    "springgreen": 0x00FF7F, "steelblue": 0x4682B4, "tan": 0xD2B48C,
    "teal": 0x008080, "thistle": 0xD8BFD8, "tomato": 0xFF6347,
    "turquoise": 0x40E0D0, "violet": 0xEE82EE, "wheat": 0xF5DEB3,
    "white": 0xFFFFFF, "whitesmoke": 0xF5F5F5, "yellow": 0xFFFF00,
    "yellowgreen": 0x9ACD32,
}

COLOUR_HELP = (
    "Give it a colour name like `sea green` or `hot pink`, or a hex code "
    "like `#ff9d2e` if you have one."
)


def parse_colour(value):
    """A colour from whatever somebody typed: a name, or hex.

    Names are tried first, because hex is the thing most people cannot
    write. Case, spaces, hyphens and underscores are all ignored, so
    "Sky Blue", "sky-blue" and "skyblue" are one colour.

    Raises ValueError on anything that is neither.
    """
    raw = (value or "").strip()
    named = COLOUR_NAMES.get(re.sub(r"[\s_-]+", "", raw).lower())
    if named is not None:
        return discord.Colour(named)
    try:
        return discord.Colour.from_str(f"#{raw.lstrip('#')}")
    except Exception as e:
        raise ValueError(str(e)) from e


def colour_name(value):
    """The name of a colour, when it has one. Used to show people back a
    word rather than the hex they did not want to deal with."""
    return next((n for n, v in COLOUR_NAMES.items() if v == value), None)


# The shortlist the dropdown offers. Every value is a name `parse_colour`
# also accepts, so the list and the box are one vocabulary rather than two:
# whatever somebody picks here, they could have typed, and the other way
# round. Discord caps a menu at 25 and will not tint the rows, so the
# squares only place a colour in its family -- the name has to do the rest.
PALETTE = [
    ("Crimson", "crimson", "🟥"), ("Red", "red", "🟥"),
    ("Tomato", "tomato", "🟥"), ("Coral", "coral", "🟧"),
    ("Orange", "orange", "🟧"), ("Gold", "gold", "🟨"),
    ("Yellow", "yellow", "🟨"), ("Lime", "limegreen", "🟩"),
    ("Green", "forestgreen", "🟩"), ("Sea green", "seagreen", "🟩"),
    ("Spring green", "springgreen", "🟩"), ("Teal", "teal", "🟦"),
    ("Turquoise", "turquoise", "🟦"), ("Sky blue", "skyblue", "🟦"),
    ("Blue", "dodgerblue", "🟦"), ("Navy", "midnightblue", "🟦"),
    ("Slate blue", "slateblue", "🟪"), ("Purple", "purple", "🟪"),
    ("Orchid", "orchid", "🟪"), ("Violet", "violet", "🟪"),
    ("Hot pink", "hotpink", "🟪"), ("Pink", "pink", "🟪"),
    ("Brown", "saddlebrown", "🟫"), ("Silver", "silver", "⬜"),
]


def colour_picker(current=None):
    """The two ways to say a colour, in the order most people need them:
    a list to pick from, and a box for anyone who already knows what they
    want. Neither is required on its own -- what an empty pair means is the
    caller's to decide, because it means "pick something" when creating a
    role and "leave it alone" when editing one."""
    swatch = discord.ui.Label(
        text="Colour",
        description="Pick one, or type your own below.",
        component=discord.ui.Select(
            required=False,
            placeholder="Choose a colour",
            options=[
                discord.SelectOption(
                    label=display, value=name, emoji=emoji,
                    default=current is not None and COLOUR_NAMES[name] == current,
                )
                for display, name, emoji in PALETTE
            ],
        ),
    )
    typed = discord.ui.TextInput(
        label="Or type a colour",
        required=False,
        max_length=32,
        placeholder="sea green, or #ff9d2e",
    )
    return swatch, typed


def picked_colour(swatch, typed):
    """What the picker came back with, or None if it came back empty.

    A typed colour wins. Somebody who went to the trouble of writing one
    meant it more than a dropdown they may never have opened, and on an
    edit the dropdown arrives with the current colour already selected.

    Raises ValueError, from `parse_colour`, on a typed colour that is
    neither a name nor hex.
    """
    written = str(typed).strip()
    if written:
        return parse_colour(written)
    chosen = swatch.component.values
    return parse_colour(chosen[0]) if chosen else None


def name_taken(guild, name, ignoring=None):
    """The role already called this, if there is one.

    Every role, not only the custom colours: two roles sharing a name are
    indistinguishable in the member list and in a mention, and which one
    somebody ends up wearing is down to whatever order Discord happens to
    return them in. Far kinder to say so before making the second one.
    """
    wanted = (name or "").strip().lower()
    ignore_id = getattr(ignoring, "id", None)
    return next(
        (r for r in guild.roles
         if r.name.strip().lower() == wanted and r.id != ignore_id),
        None,
    )


def taken_line(role, user_id):
    """Why a name is not available, said in terms of what to do next."""
    if owns_role(user_id, role):
        return (f"You already have one called **{role.name}**. Edit that one "
                f"instead of making a second.")
    if str(role.id) in role_registry():
        return (f"**{role.name}** is already somebody's colour. You can wear "
                f"it from the roles channel, or pick another name.")
    return f"**{role.name}** is already a role in this server. Pick another name."


def roles_channel(guild):
    """Where the colour buttons live. Bound like every other room, with the
    name match left underneath so a server that never bound one keeps
    working exactly as it did."""
    return (bindings.channel(guild, "wardrobe")
            or next((c for c in guild.text_channels if "roles" in c.name), None))


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
    def __init__(self):
        super().__init__()
        self.name = discord.ui.TextInput(
            label="Name", max_length=100, placeholder="What it says"
        )
        self.swatch, self.color = colour_picker()
        self.add_item(self.name)
        self.add_item(self.swatch)
        self.add_item(self.color)

    async def on_submit(self, interaction: discord.Interaction):
        name = str(self.name).strip()[:100]
        if not name or name.lower() in _protected_names():
            return await interaction.response.send_message("Pick another name.", ephemeral=True)
        clash = name_taken(interaction.guild, name)
        if clash is not None:
            return await interaction.response.send_message(
                taken_line(clash, interaction.user.id), ephemeral=True
            )
        try:
            colour = picked_colour(self.swatch, self.color)
        except ValueError:
            return await interaction.response.send_message(
                f"I do not know that colour. {COLOUR_HELP}", ephemeral=True
            )
        if colour is None:
            return await interaction.response.send_message(
                f"Pick a colour from the list, or type one. {COLOUR_HELP}",
                ephemeral=True,
            )
        if at_create_cap(interaction.user.id, interaction.guild):
            return await interaction.response.send_message(
                role_cap_line(guild=interaction.guild), ephemeral=True
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
        if len(worn_custom(interaction.user)) < numbers(interaction.guild)['role_wear_max']:
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
        # The current colour arrives already selected, and the box is left
        # empty rather than prefilled with hex: filling it would make every
        # edit of a name silently retype a colour, and typing beats the
        # dropdown, so a prefilled box would quietly ignore the swatch.
        self.swatch, self.color = colour_picker(role.colour.value)
        self.add_item(self.name)
        self.add_item(self.swatch)
        self.add_item(self.color)

    async def on_submit(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if not owns_role(interaction.user.id, role):
            return await interaction.response.send_message(
                "That role is not yours to edit.", ephemeral=True
            )
        name = str(self.name).strip()[:100] or role.name
        if name.lower() in _protected_names():
            return await interaction.response.send_message(
                "Pick another name.", ephemeral=True
            )
        clash = name_taken(interaction.guild, name, ignoring=role)
        if clash is not None:
            return await interaction.response.send_message(
                taken_line(clash, interaction.user.id), ephemeral=True
            )
        try:
            colour = picked_colour(self.swatch, self.color)
        except ValueError:
            return await interaction.response.send_message(
                f"I do not know that colour. {COLOUR_HELP}", ephemeral=True
            )
        # Nothing chosen means nothing changed: editing only the name should
        # not require saying the colour over again.
        await role.edit(name=name, colour=colour if colour is not None else role.colour)
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
        elif len(worn_custom(member)) >= numbers(member.guild)['role_wear_max']:
            worn_cap = numbers(member.guild)['role_wear_max']
            verdict = f"You are wearing {worn_cap} already. Shed one first."
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
        if at_create_cap(interaction.user.id, interaction.guild):
            return await interaction.response.send_message(
                role_cap_line(guild=interaction.guild), ephemeral=True
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


def _colour_subject(guild, member, args):
    """Who a colour tool is aimed at: the person speaking, unless they named
    somebody else. Returns (member, refusal); exactly one of them is None.

    Resolution is `powers.find_member`, which is the one used on the way to a
    timeout, and it is the one used here for the same reason: it reports an
    ambiguity instead of picking a Sam. The stakes are lower -- a colour on
    the wrong person comes off again -- but a bot that quietly guesses who
    was meant is wrong in both places, and there is no reason to keep a
    second, laxer answer to the same question.
    """
    needle = (args.get("member") or "").strip()
    if not needle:
        return member, None
    target, why = powers.find_member(guild, needle)
    if target is None:
        return None, (f"I could not work out who {needle!r} is: {why}. Ask "
                      f"them who they meant, or use their @mention.")
    return target, None


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


def _resolve_custom_role(guild, needle):
    """A colour role by name, and -- when there is none -- why not, in a
    sentence that says which of the several possible reasons it was.

    Returns (role, refusal); exactly one of them is None.

    Two quite different failures used to share one string: "No such colour
    role of theirs; only the creator may change one" was returned both for
    a name nobody has and for a name somebody else owns. A model reading it
    could not tell them apart, and it guessed the wrong one -- it told a
    member that a role they had invented the name of had been made by
    someone else. The names that do exist go back with the refusal too, so
    a wrong guess can be corrected inside the same turn instead of costing
    another exchange.
    """
    wanted = (needle or "").strip()
    if not wanted:
        return None, "No role name was given, so there is nothing to look up."
    role = _find_custom_role(guild, wanted)
    if role is not None:
        return role, None
    plain = name_taken(guild, wanted)
    if plain is not None:
        return None, (
            f"**{plain.name}** is a role in this server, but not a colour "
            f"role -- it was not made through me, so it is not mine to touch."
        )
    known = [r.name for r in custom_stack(guild)]
    if not known:
        return None, ("There are no colour roles in this server yet, so there "
                      "is nothing by that name or any other.")
    return None, (
        f"There is no colour role called {wanted!r}. The colour roles that "
        f"do exist, exactly as they are spelled: "
        + ", ".join(f"{n!r}" for n in known[:25])
        + ". Use one of those, or say that none of them is what they meant."
    )


def _not_theirs(guild, member, role, verb):
    """Why somebody may not change a role, said with the name of whoever
    may. The old wording left the model to work out who owned it, and it
    worked it out wrongly."""
    meta = role_registry().get(str(role.id))
    creator = guild.get_member(meta["creator_id"]) if meta else None
    who = creator.display_name if creator else "somebody who has since left"
    return (
        f"**{role.name}** was made by {who}, not by {member.display_name}, "
        f"so only {who} may {verb} it. {member.display_name} may wear it, "
        f"or make one of their own."
    )


async def act_list_colors(guild, member, args):
    """The wardrobe, and -- kept separate from it -- what the person asking
    has and may have.

    Three things were missing and all three were guessed at instead. The
    colour went out as bare hex, and #ffa500 came back to a member as
    "tomato red". Nothing said whether the person asking was *wearing*
    anything, only how many people were, so "you have a role" and "you are
    wearing a role" collapsed into one sentence and the member had to
    correct him. And the caps were nowhere, so he offered to make a second
    role in a house that allows one. All three are fields now: a model that
    is told a thing does not have to invent it.
    """
    registry = role_registry()
    caps = numbers(guild)
    wardrobe = []
    for r in custom_stack(guild):
        creator = guild.get_member(registry[str(r.id)]["creator_id"])
        wardrobe.append(
            {
                "name": r.name,
                "colour": colour_name(r.colour.value) or f"#{r.colour.value:06x}",
                "hex": f"#{r.colour.value:06x}",
                "wearers": len([m for m in r.members if not m.bot]),
                "made_by": creator.display_name if creator else "someone departed",
                "made_by_them": creator.id == member.id if creator else False,
                "worn_by_them": r in member.roles,
            }
        )
    made = [w["name"] for w in wardrobe if w["made_by_them"]]
    worn = [w["name"] for w in wardrobe if w["worn_by_them"]]
    return json.dumps(
        {
            "who_is_asking": member.display_name,
            "roles_they_made": made,
            "roles_they_are_wearing": worn,
            "may_make": caps["role_create_max"],
            "may_wear_at_once": caps["role_wear_max"],
            "can_make_another": not at_create_cap(member.id, guild),
            "can_wear_another": len(worn) < caps["role_wear_max"],
            "note": "made and worn are different things: somebody can own a "
                    "colour they have taken off, and wear one somebody else "
                    "made. Say the name exactly as it is spelled here.",
            "wardrobe": wardrobe,
        }
    )


async def act_create_color(guild, member, args):
    # Made for somebody else, it is still the asker's role and still counts
    # against the asker's allowance: the wearer did not ask for it and must
    # not lose one of their own five to somebody else's present.
    wearer, refusal = _colour_subject(guild, member, args)
    if refusal:
        return refusal
    name = (args.get("name") or "").strip()[:100]
    if not name or name.lower() in _protected_names():
        return "That name is not available."
    # Asked for before anything is made, so a second one is never quietly
    # created alongside the first.
    clash = name_taken(guild, name)
    if clash is not None:
        return taken_line(clash, member.id)
    if at_create_cap(member.id, guild):
        # The shared refusal says the rule; it cannot say which role has to
        # go, because the panel and the modal that also use it are talking
        # to somebody who can see their own roles. Eugene is not, so the
        # names go on the end -- he named the wrong role once already.
        theirs = [
            r.name for r in custom_stack(guild)
            if role_registry().get(str(r.id), {}).get("creator_id") == member.id
        ]
        line = role_cap_line(member.display_name, guild)
        if theirs:
            line += (f" What {member.display_name} has already made: "
                     f"{', '.join(repr(n) for n in theirs)}. Recolouring one "
                     f"of those is very likely what they actually want; "
                     f"deleting is not something to do without being asked.")
        return line
    try:
        colour = parse_colour(args.get("color", ""))
    except ValueError:
        return f"I do not know that colour. {COLOUR_HELP}"
    role = await guild.create_role(
        name=name, colour=colour, permissions=discord.Permissions.none(),
        mentionable=False, hoist=False,
        reason=f"Colour role for {member.display_name}, via Eugene",
    )
    registry = role_registry()
    registry[str(role.id)] = {"creator_id": member.id}
    save_json(ROLES, registry)
    worn = ""
    if len(worn_custom(wearer)) < numbers(guild)['role_wear_max']:
        await wearer.add_roles(
            role,
            reason=("Creator wears their creation" if wearer.id == member.id
                    else f"Made for them by {member.display_name}"),
        )
        worn = (" and is wearing it" if wearer.id == member.id
                else f" and it is on {wearer.display_name}")
    else:
        # A colour nobody can see is worth saying out loud, or the answer is
        # "created it" and the person it was made for sees no change at all.
        worn = (f", but {wearer.display_name} is already wearing "
                f"{numbers(guild)['role_wear_max']}, so it will not show "
                f"until one comes off")
    await ensure_color_stack(guild)
    await update_wardrobe(guild)
    shown = colour_name(colour.value) or f"#{colour.value:06x}"
    return f"Created {role.name} ({shown}) for {wearer.display_name}{worn}."


async def act_edit_color(guild, member, args):
    role, refusal = _resolve_custom_role(guild, args.get("role"))
    if refusal:
        return refusal
    if not owns_role(member.id, role):
        return _not_theirs(guild, member, role, "change")
    kwargs = {}
    new_name = (args.get("name") or "").strip()
    if new_name and new_name.lower() not in _protected_names():
        clash = name_taken(guild, new_name, ignoring=role)
        if clash is not None:
            return taken_line(clash, member.id)
        kwargs["name"] = new_name[:100]
    if args.get("color"):
        try:
            kwargs["colour"] = parse_colour(args["color"])
        except ValueError:
            return f"I do not know that colour. {COLOUR_HELP}"
    if not kwargs:
        return ("Nothing to change: no new name and no new colour were given. "
                "Ask them which they meant.")
    # discord.py hands back the edited role; older ones edited in place and
    # returned nothing, and reading the colour off a stale object is how a
    # confirmation ends up naming the old one.
    role = (await role.edit(**kwargs)) or role
    # A recolour nobody can see is not a recolour. Somebody who asks for
    # their colour to be purple wants to *be* purple, and the honest report
    # of "Updated Horsy." was followed by "i dont have any roles" -- true
    # both times, and useless. Same bargain as creating one: if there is
    # room, it goes on, and either way the answer says which happened.
    wearing = ""
    if role not in member.roles:
        if len(worn_custom(member)) < numbers(guild)["role_wear_max"]:
            await member.add_roles(role, reason="Wearing their own recoloured role")
            wearing = (f" {member.display_name} was not wearing it, so it is "
                       f"on them now and the colour actually shows.")
        else:
            wearing = (f" {member.display_name} is not wearing it, and is "
                       f"already wearing {numbers(guild)['role_wear_max']}, so "
                       f"the new colour will not show until one comes off. "
                       f"Say so.")
    await ensure_color_stack(guild)
    await update_wardrobe(guild)
    shown = colour_name(role.colour.value) or f"#{role.colour.value:06x}"
    return f"Updated {role.name}; it is {shown} now.{wearing}"


async def act_delete_color(guild, member, args):
    role, refusal = _resolve_custom_role(guild, args.get("role"))
    if refusal:
        return refusal
    if not owns_role(member.id, role):
        return _not_theirs(guild, member, role, "delete")
    registry = role_registry()
    registry.pop(str(role.id), None)
    save_json(ROLES, registry)
    name = role.name
    await role.delete(reason=f"Deleted by creator {member.display_name}, via Eugene")
    await update_wardrobe(guild)
    return f"Deleted {name}."


async def act_wear_color(guild, member, args):
    """Colours are cosmetic and reversible, so anyone may put one on anyone.
    The wearer can always take it off again."""
    target, refusal = _colour_subject(guild, member, args)
    if refusal:
        return refusal
    role, refusal = _resolve_custom_role(guild, args.get("role"))
    if refusal:
        return refusal
    if role in target.roles:
        return f"{target.display_name} is already wearing {role.name}."
    worn = worn_custom(target)
    if len(worn) >= numbers(guild)['role_wear_max']:
        # Naming what is in the way is the difference between a refusal
        # they can act on and one they have to ask a second question about.
        return (f"{target.display_name} is wearing "
                f"{numbers(guild)['role_wear_max']} already, which is the "
                f"limit: {', '.join(repr(r.name) for r in worn)}. One has to "
                f"come off first -- ask which, do not pick for them.")
    await target.add_roles(
        role,
        reason=("Worn via Eugene" if target.id == member.id
                else f"Put on via Eugene by {member.display_name}"),
    )
    await update_wardrobe(guild)
    shown = colour_name(role.colour.value) or f"#{role.colour.value:06x}"
    return f"{target.display_name} is now wearing {role.name} ({shown})."


async def act_shed_color(guild, member, args):
    """Off yourself always, and off anybody else only if you made the role.
    Anything looser and one person can strip another's colours for fun."""
    target, refusal = _colour_subject(guild, member, args)
    if refusal:
        return refusal
    role, refusal = _resolve_custom_role(guild, args.get("role"))
    if refusal:
        return refusal
    if target.id != member.id and not owns_role(member.id, role):
        return (f"{role.name} is not {member.display_name}'s to take off "
                f"{target.display_name}: only the person wearing a colour, or "
                f"whoever made it, can take it off.")
    if role not in target.roles:
        worn = [r.name for r in worn_custom(target)]
        return (f"{target.display_name} is not wearing {role.name}, so there "
                f"is nothing to take off. "
                + (f"They are wearing: {', '.join(repr(n) for n in worn)}."
                   if worn else "They are not wearing any colour role."))
    await target.remove_roles(
        role,
        reason=("Shed via Eugene" if target.id == member.id
                else f"Taken off via Eugene by {member.display_name}"),
    )
    await update_wardrobe(guild)
    return f"{target.display_name} took off {role.name}."


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


async def act_what_you_know(guild, invoker, args):
    """What he has about the person asking. Never about anybody else, and
    the handler is where that is enforced rather than the prompt: `who` is
    not a parameter, so there is nothing to talk him into."""
    if people.is_closed(invoker.id):
        return json.dumps({
            "about": invoker.display_name,
            "notes": [],
            "learning": False,
            "note": "They asked you to stop, and you did. Say so.",
        })
    profile = people.profile(invoker.id)
    return json.dumps({
        "about": invoker.display_name,
        "notes": [n["text"] for n in profile.get("notes", [])],
        "learning": True,
        "note": "Read it back plainly if they asked. It is theirs to delete "
                "with forget_about_me, and you never argue about that.",
    })


async def act_forget_about_me(guild, invoker, args):
    """Strike everything about the asker, and stop.

    Member-tier and instant, like every other thing here that acts on the
    asker inside their own powers. Nobody may strike anybody else's: there
    is no id to pass, so there is nothing to get wrong.
    """
    on = args.get("learning")
    if isinstance(on, str):
        on = on.strip().lower() not in ("false", "no", "off", "0")
    if on:
        people.reopen(invoker.id)
        log.info(f"{invoker.display_name} let him start learning again")
        return json.dumps({
            "learning": True,
            "note": "Starting again from here. Nothing old came back.",
        })
    gone = people.forget_person(invoker.id, display=invoker.display_name)
    log.info(f"{invoker.display_name} struck their profile ({gone} note(s))")
    return json.dumps({
        "struck": gone,
        "learning": False,
        "note": "Gone -- their notes and their name off the house shelf "
                "with them -- and you have stopped learning about them. No "
                "argument, no asking why, and do not offer to keep any of it.",
    })


DUTY_ACTIONS = {
    "set_nudges": act_set_nudges,
    "mark_carried_out": act_mark_carried_out,
    "what_you_know": act_what_you_know,
    "forget_about_me": act_forget_about_me,
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
            f"long look on `{brain.deep_model_name(guild.id, name)}`, "
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

    @discord.ui.button(label="What needs doing", style=discord.ButtonStyle.primary, row=2)
    async def survey_button(self, interaction, button):
        await interaction.response.edit_message(
            content=survey.render(look(interaction.guild), limit=1850)
            + "\n-# `/survey deep: True` has him read this and say which of "
              "it actually matters.",
            view=StewardView(self.owner_id),
        )

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
    def __init__(self, key, row, page=0):
        super().__init__(
            channel_types=[discord.ChannelType.text],
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
    """The bootstrap door, and only that.

    Once there is a cooperative the way in is `/invite` and a vote; this is
    not a shortcut around it, and the screen says so. But a server with
    nobody inside cannot hold a vote about letting somebody in, so the
    first few are handed over by whoever runs the place.
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
            note + "\n-# This is the bootstrap door. Once there are a few of "
                   "you, `/invite` is the ordinary way in, and it is a vote.",
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
    # Two models, because they are answering different questions at
    # different prices. He replies to a mention in a sentence or two, which
    # a cheap model does perfectly well and a frontier one does at several
    # times the cost; the long look reads twenty findings at once and says
    # which three matter, which is exactly what cheap models are worst at.
    # One field for both means either every "morning" costs a fortune or
    # the audit is answered by something that cannot hold it in its head.
    deep = discord.ui.TextInput(
        label="Model for the long look (blank for the best)",
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
        self.deep.placeholder = providers.deep_model(annex)
        current = settings.model(guild.id, annex)
        if current:
            self.model.default = current
        chosen = settings.deep_model(guild.id, annex)
        if chosen:
            self.deep.default = chosen

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
        settings.set_deep_model(self.guild.id, self.annex,
                                str(self.deep).strip())
        brain.forget_client(self.guild.id)
        log.info(
            f"{self.annex} key set for guild {self.guild.id} by "
            f"{interaction.user.display_name} ({settings.fingerprint(key)})"
        )
        others = [
            n for n in settings.keyed_providers(self.guild.id, providers.NAMES)
            if n != self.annex
        ]
        woken = [modules.name(k) for k in ("chat", "memory", "pulse")
                 if modules.enabled(self.guild.id, k)]
        await interaction.followup.send(
            f"Done. He is awake through {providers.label(self.annex)} on "
            f"`{model}`, and the key ({settings.fingerprint(key)}) stays here.\n"
            + (f"-# That wakes {', '.join(woken).lower()}.\n" if woken else "")
            + f"-# The long look (`/survey deep: True`) uses "
              f"`{brain.deep_model_name(self.guild.id, self.annex)}`.\n"
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
        "-# Two models per key: a cheap one for talking, and a good one for "
        "the long look (`/survey deep: True`), which is the only thing he "
        "does that is worth a frontier model.",
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


# ---------- the long look ----------
# The one thing he does by looking rather than by being asked a question.
# `survey.py` holds every rule and is Discord-free; this is the walk round
# the building that feeds it, and it is the only place in the clerk that
# reads the whole server at once.
#
# All of it is free. Nothing below spends a token, and the report is the
# same report whether or not this server has ever bought a key. What a
# brain adds is judgement over the list, and that is asked for by name.


def _quiet_days(channel):
    """Days since anything was said, from the id of the last message.

    Off the id rather than by fetching history: a Discord snowflake carries
    its own timestamp, so this costs no API call at all and works for a
    hundred channels as cheaply as for one. A channel nobody has ever
    posted in reads as None rather than as infinitely quiet, because those
    are different things and only one of them is a finding.
    """
    last = getattr(channel, "last_message_id", None)
    if not last:
        return None
    try:
        when = discord.utils.snowflake_time(last)
    except (ValueError, OverflowError, TypeError):
        return None
    return max(0, (now_utc() - when).days)


def _arrivals_facts(guild):
    """What already greets people here.

    Discord's own join notices are a flag on the guild and cost nothing to
    read. Another bot doing the same job is not visible from here and this
    does not pretend otherwise -- what it can see, it says.
    """
    flags = getattr(guild, "system_channel_flags", None)
    system = guild.system_channel
    # discord.py already inverts this one for us: Discord's own bit is
    # SUPPRESS_JOIN_NOTIFICATIONS, and `join_notifications` reads True when
    # the notices are on. Reading it as the raw suppression bit gets the
    # answer backwards, which is a finding that fires for every server that
    # has already turned Discord's greeting off.
    greets = bool(system is not None and flags is not None
                  and flags.join_notifications)
    mine = bindings.channel(guild, "welcome")
    return {
        "on": modules.enabled(guild.id, "welcome"),
        "room": mine.name if mine is not None else None,
        "discord_greets": greets,
        "discord_room": system.name if system is not None else None,
    }


def gather(guild):
    """Everything `survey.py` needs, as plain data.

    The split is the same one the rest of the clerk uses: the walk is here
    because it needs a guild, and every judgement about what the walk found
    is over there because judgements should be testable without one.
    """
    me = guild.me
    coop = cooperative_role(guild)
    inside = cooperative_members(guild)
    registry = role_registry()

    outranked = []
    if me is not None:
        outranked = [r.name for r in guild.roles
                     if r > me.top_role and not r.is_default()][:8]

    channels = []
    for channel in guild.text_channels:
        channels.append({
            "id": channel.id,
            "name": channel.name,
            "claims": bindings.job_of(channel.name),
            "archived": channel.name.startswith("archived_"),
            "quiet_days": _quiet_days(channel),
            "messages": None,
        })

    room_health, room_owners = {}, {}
    for job in bindings.ROOMS:
        found = bindings.channel(guild, job)
        owner = next((k for k in modules.keys()
                      if job in modules.spec(k)["rooms"]), None)
        room_owners[job] = ({"key": owner,
                             "enabled": modules.enabled(guild.id, owner)}
                            if owner else None)
        if found is not None and me is not None:
            allowed = found.permissions_for(me)
            room_health[job] = {"cannot_post": not allowed.send_messages}

    bills = load_json(BILLS, [])
    open_bills = [b for b in bills if b.get("status") == "on_floor"]
    stale = [f"No. {b['no']} {b['title']}" for b in open_bills
             if b.get("ends_at")
             and datetime.fromisoformat(b["ends_at"]) < now_utc() - timedelta(hours=1)]
    filed = [b.get("filed_at") for b in bills if b.get("filed_at")]
    quiet_floor = None
    if filed:
        quiet_floor = (now_utc() - datetime.fromisoformat(max(filed))).days

    ghosts, orphans, unworn = [], [], []
    for role_id, entry in registry.items():
        role = guild.get_role(int(role_id))
        if role is None:
            ghosts.append(str(role_id))
            continue
        if guild.get_member(entry.get("creator_id") or 0) is None:
            orphans.append(role.name)
        if not role.members:
            unworn.append(role.name)

    states = {}
    for key in modules.keys():
        states[key] = {
            "name": modules.name(key),
            "state": module_state(guild, key),
            "blockers": module_blockers(guild, key),
        }

    optional = [job for job in modules.wanted_rooms(guild.id)
                if bindings.channel(guild, job) is None
                and job not in modules.required_rooms(guild.id)]

    return {
        "guild": {
            "name": guild.name,
            "members": guild.member_count,
            "described": bool(settings.get(guild.id, "house")),
            # "talking" keeps one finding honest: an empty floor in a dead
            # server is not a governance problem, it is a dead server.
            "talking": any(c["quiet_days"] is not None and c["quiet_days"] < 7
                           for c in channels),
        },
        "me": {
            "missing_permissions": builder.missing_permissions(guild),
            "outranked_by": outranked,
            "message_content": intents.message_content,
        },
        "chat_on": modules.enabled(guild.id, "chat"),
        "arrivals": _arrivals_facts(guild),
        "modules": states,
        "bindings": {"rooms": {j: bindings.bound_channel_id(guild.id, j)
                               for j in bindings.ROOMS}},
        "room_health": room_health,
        "room_owners": room_owners,
        "optional_unbound": optional,
        "channels": channels,
        "categories": [{"name": c.name, "channels": len(c.channels)}
                       for c in guild.categories],
        "cooperative": {
            "size": len(inside),
            "away": sum(
                1 for m in inside
                if roster.away_reason(m, numbers(guild)["away_days"])
            ),
        },
        "record": {
            "outstanding": [i.get("title", "?") if isinstance(i, dict) else str(i)
                            for i in duties.outstanding(bills, closing_report)],
            "stale_open": stale,
            "floor_quiet_days": quiet_floor,
        },
        "colours": {"registered_but_gone": ghosts, "owner_left": orphans,
                    "unworn": unworn},
        "brain": {
            "provider": brain.provider_name(guild.id),
            "spent": brain.spend_usd(guild.id),
            "budget": settings.budget_usd(guild.id),
            "deep_better": brain.deep_is_better(guild.id),
        },
        "slate": {"stranded_from": slate.owner() if slate.stranded(guild.id)
                  else None},
    }


def look(guild):
    """The findings, for nothing."""
    return survey.inspect(gather(guild))


async def act_survey(guild, invoker, args):
    """The same findings the command prints, for the model to judge.

    Ungated by feature on purpose: "what is wrong with my server" is a
    question worth answering in a server that has switched almost
    everything off, and the answer is often that they switched something
    off.
    """
    grade = str(args.get("grade") or "all").strip().lower()
    findings = look(guild)
    if grade in survey.GRADES:
        findings = [f for f in findings if f["grade"] == grade]
    return json.dumps({
        "tally": survey.tally(look(guild)),
        "findings": survey.brief(findings),
        "note": "worked out in code, not remembered; current as of now. "
                "Say which few matter, not all of them.",
    })


SURVEY_ACTIONS = {"survey_server": act_survey}


@bot.tree.command(
    name="survey",
    description="What is broken here, what is missing, and what wants tidying",
)
@app_commands.describe(
    deep="Have him read the findings and say what actually matters. Costs.",
    question="Something specific to ask about them, e.g. what needs cleaning",
)
@app_commands.guild_only()
async def slash_survey(interaction: discord.Interaction,
                       deep: bool = False, question: str = ""):
    """The long look.

    Free by default and free by design: every finding is worked out in
    plain Python over facts already in memory, so a house with no key, or
    one whose bill has run out for the month, gets exactly the same list.

    `deep: True` is the other half, and it is the one thing in the clerk
    that deliberately reaches for an expensive model. The list is
    exhaustive rather than considered -- nineteen true things in no
    meaningful order -- and turning that into "do these three, ignore the
    rest, and here is why" is judgement over a lot of context at once,
    which is the job cheap models are worst at and the reason a good one
    is worth a few cents here and nowhere else.
    """
    if not keyed_in(interaction):
        return await refuse(interaction, "That one is for people who are in.")
    await interaction.response.defer(ephemeral=True, thinking=True)
    findings = look(interaction.guild)
    if not deep:
        tail = ("\n-# `/survey deep: True` has me read this and say what "
                "actually matters." if brain.enabled(interaction.guild.id)
                else "")
        return await interaction.followup.send(
            survey.render(findings, limit=1850) + tail, ephemeral=True
        )
    denial = brain.may_spend(interaction.guild.id, interaction.user.id)
    if denial:
        return await interaction.followup.send(
            survey.render(findings, limit=1800) + f"\n-# {denial}",
            ephemeral=True,
        )
    try:
        answer, cost = await brain.long_look(
            interaction.guild, survey.brief(findings), asked=question or None
        )
    except Exception as e:
        log.error(f"long look failed: {e!r}")
        return await interaction.followup.send(
            survey.render(findings, limit=1850)
            + "\n-# The annex was unreachable, so that is the list without "
              "my read on it.",
            ephemeral=True,
        )
    log.info(f"long look in {interaction.guild.id} by "
             f"{interaction.user.display_name}: ${cost:.4f}, "
             f"{len(findings)} finding(s)")
    counts = survey.tally(findings)
    head = " · ".join(f"{survey.MARKS[g]} {counts[g]}" for g in survey.GRADES
                      if counts[g])
    await interaction.followup.send(
        f"## The long look at {interaction.guild.name}\n"
        f"-# {head} · `{brain.deep_model_name(interaction.guild.id)}` · "
        f"${cost:.3f}\n\n"
        + (answer or "He had nothing to add.")[:1700]
        + "\n-# `/survey` on its own prints the findings themselves, free.",
        ephemeral=True,
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
    "role_create_max": "colour roles one person may make",
    "role_wear_max": "colour roles one person may wear at once",
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
    # The debate chambers a vote opens want a home, and the governance
    # category is the obvious one. Meek, like everything else here: it never
    # re-points a key somebody has already bound by hand.
    if "governance" in made and bindings.category(guild, "chambers") is None:
        bindings.bind_category(guild.id, "chambers", made["governance"].id)
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
        # is meant here -- inherit the category, touch nothing -- and it is
        # what open_chamber_overwrites has always returned for the same
        # reason. This crashed `/setup` on the first room that is open
        # to everybody, which is to say on every fresh install.
        overwrites = {}
        if spec["visibility"] == "cooperative" and coop is not None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                coop: discord.PermissionOverwrite(view_channel=True),
            }
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
        "have one. After that the ordinary way in is `/invite`, which is a vote."
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


@bot.tree.command(name="invite", description="Propose that someone be let in")
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


@bot.tree.command(
    name="whatdoyouknow",
    description="Everything Eugene has picked up about you, and a way to delete it",
)
@app_commands.describe(
    forget="Strike the lot. He stops learning about you until you say otherwise."
)
@app_commands.guild_only()
async def slash_whatdoyouknow(interaction: discord.Interaction,
                              forget: bool = False):
    """Your own profile, and nobody else's, ever.

    He learns people from ordinary conversation now, which is a real thing
    to do to somebody. The two things that make it fair are that you can
    read exactly what he has and delete it, and both live here. There is no
    argument and no "are you sure": a delete somebody has to justify is not
    one they really have.
    """
    if not in_room(interaction.user):
        return await refuse(interaction, NOT_INSIDE)
    # Deleting still works with the feature off: notes he took while it was
    # on are still his to hold and still yours to strike. Only the reading
    # and the learning stop.
    if not forget and await refuse_unless(interaction, "memory"):
        return
    if forget:
        gone = people.forget_person(interaction.user.id)
        log.info(f"{interaction.user.display_name} struck their profile "
                 f"({gone} note(s))")
        return await interaction.response.send_message(
            f"Struck {gone} note(s), and I have stopped learning about you. "
            f"Nothing of you goes in the book until you run this with "
            f"`forget: False`.",
            ephemeral=True,
        )
    if people.is_closed(interaction.user.id):
        # Coming back is the same command without the flag, and it takes
        # effect rather than explaining how to make it take effect.
        people.reopen(interaction.user.id)
        log.info(f"{interaction.user.display_name} let him start learning again")
        return await interaction.response.send_message(
            "You had asked me to stop, so I had nothing. I will start "
            "again from here — run this with `forget: True` whenever you "
            "want it gone.",
            ephemeral=True,
        )
    await interaction.response.send_message(
        f"**What I have about you**\n{people.summary(interaction.user.id)}",
        ephemeral=True,
    )


@bot.tree.command(name="role", description="Make or manage your colour role")
@app_commands.guild_only()
async def slash_role(interaction: discord.Interaction):
    if await refuse_unless(interaction, "colours"):
        return
    if not keyed_in(interaction):
        return await refuse(interaction, "That one is for people who are in.")
    # Always the whole wardrobe, never straight into the create modal. A
    # member gets one colour of their own, and someone who typed /role
    # meaning to wear a colour already on the rack must not be handed the
    # form that spends it.
    await interaction.response.send_message(
        "Your wardrobe.", view=RolesHomeView(), ephemeral=True
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
            + f"\n{rungs}\n-# `/model tier: opus` moves him. The long look "
              f"(`/survey deep: True`) has its own model and is not touched.",
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
          f"${settings.budget_usd(guild.id):.0f} spent this month.\n"
        + f"-# The long look still uses "
          f"`{brain.deep_model_name(guild.id, 'claude')}`.",
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
    # Before the harness, and that order is load-bearing now: `toolbox`
    # folds any old memory book into this store on the way up, and a store
    # that has not been told where it lives accepts the writes and drops
    # them on the floor.
    people.configure(DATA)
    toolbox.configure(
        HERE, DATA,
        {**COLOR_ACTIONS, **BILL_ACTIONS, **DUTY_ACTIONS, **SURVEY_ACTIONS,
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
    pulse.configure(DATA)
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
    bot.add_view(RolesHomeView())
    bot.add_view(DraftView())
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
    if module_live(guild, "colours"):
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
    if module_live(guild, "health"):
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
    if not pulse_loop.is_running():
        pulse_loop.start()


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
