"""The sign-off desk: the second hand on every moderation action.

powers.py is the hands, and until now the hands moved the moment somebody
in the cooperative asked. That was a deliberate choice and it is written
up at length in that file: the confirmation had already happened when the
house decided who was in the cooperative, and a tool that asks permission
twice is a tool people route around.

The house has changed its mind. A warning, a ban, a swept channel and a
locked room now wait for an administrator to sign them off. What that
buys is not caution -- the cooperative is still trusted -- but a second
pair of eyes on the one class of action that cannot be undone by saying
sorry, and a paper record of who agreed to it that is separate from who
asked.

The shape of it is a desk, not a doorway. Eugene does not stand there
holding the conversation open while an administrator is found; a turn
that blocks for an hour is a turn that dies, and the person who asked
watches a typing indicator until it does. Instead the request is written
down, a card goes up in the log room with two buttons on it, and Eugene
says plainly that it is filed. When an administrator presses Approve the
action runs *then*, out of the conversation entirely, and the card
becomes the receipt. Nothing is executed at filing time, so a request
that lapses leaves no case in the book -- the book stays a list of things
that happened.

Three things are re-checked at the moment of the press rather than at the
moment of the asking, because a card outlives the conversation that made
it:

  - that the signer is still an administrator, for the same reason the
    setup panel re-checks it -- someone can be demoted between the card
    going up and the button going down;
  - that whoever asked is still in the cooperative, so a request does not
    outlive the standing of the person who made it;
  - that the request has not lapsed, which is a setting, because an hour
    is right for a busy server and a week is right for a quiet one.

The signer may be the same person who asked. In a house with one
administrator any other rule is a house that cannot moderate itself, and
the press is still a deliberate, out-of-band act against a card that
spells out exactly what is about to happen. It is recorded either way,
and the log line says when the two hands were the same one.

Automod is not routed through here. The filters act on a message they
have just deleted, in the second after it was posted, and a spam wave
that waits for a signature is a spam wave that worked. Those actions were
never anybody's word in the first place -- they are the settings doing
what the settings say -- and they stay immediate.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import discord

import bindings
import settings
import warden

log = logging.getLogger("sanction")

_deps = {}  # injected by clerk.py
_executors = {}  # tool name -> the coroutine that actually does it

PENDING = "warden_signoffs.json"
CAP = 200  # a desk, not an archive: old decisions age out of the file


def configure(bot, is_admin):
    _deps.update(bot=bot, is_admin=is_admin)


def register(tool, executor):
    """powers.py hands over the real handler under its tool name.

    Going through a name rather than storing the callable in the record
    is what lets a request survive a restart: a function cannot be
    written to JSON, and a card that came back from disk still has to
    find its way to the same handler it was filed against.
    """
    _executors[tool] = executor


def now():
    return datetime.now(timezone.utc)


# ---------- the store ----------

def _atomic(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _read(guild_id):
    path = settings.state_file(guild_id, PENDING)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # A corrupt desk must not take the server down. It does mean the
        # cards on screen go dead, which is why they say so when pressed
        # rather than silently doing nothing.
        log.warning(f"the sign-off desk for guild {guild_id} is unreadable; "
                    f"starting fresh")
        return []


def _write(guild_id, rows):
    _atomic(settings.state_file(guild_id, PENDING), rows[-CAP:])


def _save(guild_id, record):
    rows = [r for r in _read(guild_id) if r["id"] != record["id"]]
    rows.append(record)
    rows.sort(key=lambda r: r["id"])
    _write(guild_id, rows)


def by_message(guild_id, message_id):
    for row in _read(guild_id):
        if row.get("message_id") == message_id:
            return row
    return None


def pending(guild_id):
    return [r for r in _read(guild_id) if r["state"] == "pending"]


# ---------- where the card goes ----------

def desk(guild):
    """The room a request is posted in.

    The log room first, because that is where this house already reads
    what Eugene did. The chain past it exists so that a server which has
    configured nothing still gets a desk: a gate with nowhere to post is
    a gate that refuses every moderation action there is, and a house
    that cannot moderate is worse off than one whose sign-off cards
    landed somewhere slightly wrong.
    """
    cid = warden.get(guild.id, "log.channel")
    for channel in (guild.get_channel(cid) if cid else None,
                    bindings.channel(guild, "health"),
                    getattr(guild, "system_channel", None)):
        if channel is not None and _postable(guild, channel):
            return channel
    for channel in getattr(guild, "text_channels", []):
        if _postable(guild, channel):
            return channel
    return None


def _postable(guild, channel):
    me = getattr(guild, "me", None)
    if me is None or not hasattr(channel, "permissions_for"):
        return hasattr(channel, "send")
    try:
        return channel.permissions_for(me).send_messages
    except Exception:  # a permission model we do not recognise is not a no
        return True


# ---------- filing ----------

def _err(text):
    return json.dumps({"error": text})


def required(guild_id):
    return bool(warden.get(guild_id, "mod.require_signoff"))


def _minutes(guild_id):
    return int(warden.get(guild_id, "mod.signoff_minutes") or 60)


async def gate(guild, invoker, tool, args, executor, headline, detail=None):
    """Either file this for an administrator, or let it through.

    Let it through in exactly two cases: the house has switched the
    requirement off, or there is no guild to have switched it off in --
    the second being the tests and the console, where there is no Discord
    to post a card to and nothing real to sign.
    """
    if guild is None or not required(guild.id):
        return await executor(guild, invoker, args)

    channel = desk(guild)
    if channel is None:
        return _err("that needs an administrator's sign-off, and I have "
                    "nowhere to post the request — give me a room I can "
                    "write in, or set log.channel")

    rows = _read(guild.id)
    minutes = _minutes(guild.id)

    # The same person asking for the same thing twice is one request, not
    # two. A model that does not understand "filed" calls the tool again
    # inside the same turn, and two cards for one ban is two chances to
    # ban somebody once -- an administrator who approves both has done
    # something nobody asked for.
    for row in rows:
        if (row["state"] == "pending" and not _expired(row)
                and row["tool"] == tool and row["args"] == args
                and row["asked_by"] == getattr(invoker, "id", None)):
            log.info(f"sign-off #{row['id']} asked for again; not filed twice")
            return _filed(row, guild, minutes, again=True)

    record = {
        "id": (rows[-1]["id"] + 1) if rows else 1,
        "tool": tool,
        "args": args,
        "headline": headline,
        "detail": detail,
        "asked_by": getattr(invoker, "id", None),
        "asked_by_name": getattr(invoker, "display_name", str(invoker)),
        "filed_at": now().isoformat(),
        "expires_at": (now() + timedelta(minutes=minutes)).isoformat(),
        "state": "pending",
        "channel_id": channel.id,
        "message_id": None,
        "signed_by": None,
        "signed_at": None,
        "outcome": None,
    }
    try:
        message = await channel.send(_card(record), view=SignOffView())
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(f"could not post a sign-off card: {e!r}")
        return _err("that needs an administrator's sign-off and I could not "
                    "post the request; nothing has been done")
    record["message_id"] = message.id
    _save(guild.id, record)
    log.info(f"sign-off #{record['id']} filed by {record['asked_by_name']}: "
             f"{headline}")
    return _filed(record, guild, minutes)


def _filed(record, guild, minutes, again=False):
    where = getattr(guild.get_channel(record["channel_id"]), "name",
                    "the log room")
    return json.dumps({
        "filed_for_sign_off": record["id"],
        "what": record["headline"],
        "done": False,
        "where": where,
        "stands_for_minutes": minutes,
        # The model is told what to say rather than left to invent it,
        # because the one thing it must not imply is that the thing
        # happened. It has not. Nobody has been banned yet.
        "tell_them": (
            f"you already filed this one; say it is still waiting"
            if again else
            f"say this is not done yet: it is written up in #{where} and "
            f"waiting on an administrator to sign it off"),
    })


# ---------- the card ----------

def _stamp(iso):
    try:
        return f"<t:{int(datetime.fromisoformat(iso).timestamp())}:R>"
    except (TypeError, ValueError):
        return "shortly"


def _card(record):
    lines = [
        f"🖊️ **Sign-off #{record['id']} — {record['headline']}**",
        f"asked for by {record['asked_by_name']}",
    ]
    if record.get("detail"):
        lines.append(f"-# {record['detail']}")
    state = record["state"]
    if state == "pending":
        lines.append(f"-# Nothing has happened yet. An administrator has to "
                     f"approve it. Lapses {_stamp(record['expires_at'])}.")
    elif state == "approved":
        same = " (the same hand that asked)" if \
            record.get("signed_by") == record.get("asked_by") else ""
        lines.append(f"✅ Approved by {record['signed_by_name']}{same}.")
        if record.get("outcome"):
            lines.append(f"-# {record['outcome']}")
    elif state == "denied":
        lines.append(f"✋ Denied by {record['signed_by_name']}. Nothing was done.")
    elif state == "lapsed":
        lines.append("🕓 Nobody signed it off in time, so it lapsed. Nothing "
                     "was done.")
    return "\n".join(lines)[:1900]


async def _redraw(guild, record, view=None):
    channel = guild.get_channel(record.get("channel_id"))
    if channel is None or not record.get("message_id"):
        return
    try:
        message = await channel.fetch_message(record["message_id"])
        await message.edit(content=_card(record), view=view)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        log.info(f"could not redraw sign-off #{record['id']}: {e!r}")


# ---------- the press ----------

REFUSAL = ("Sign-off is an administrator's, sorry. Not a judgement about "
           "you — it is the same rule for everyone who is not one.")


def _is_admin(member):
    check = _deps.get("is_admin")
    if check is None:
        # A host that never said who administrates gets the strict
        # reading, for the same reason the officer gate does: the
        # question here is "may this person approve a ban".
        return False
    try:
        return bool(check(member))
    except Exception as e:
        log.error(f"the administrator check failed: {e!r}")
        return False


def _expired(record):
    try:
        return datetime.fromisoformat(record["expires_at"]) <= now()
    except (KeyError, TypeError, ValueError):
        return False


class SignOffView(discord.ui.View):
    """Two buttons that outlive the process that drew them. Registered at
    boot like every other view here, so a deploy in the middle of a
    pending request leaves the card working rather than dead."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Approve", emoji="✅", style=discord.ButtonStyle.success,
        custom_id="clerk:signoff_ok",
    )
    async def approve(self, interaction, button):
        await press(interaction, True)

    @discord.ui.button(
        label="Deny", emoji="✋", style=discord.ButtonStyle.danger,
        custom_id="clerk:signoff_no",
    )
    async def deny(self, interaction, button):
        await press(interaction, False)


async def press(interaction, approved):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message(
            "That card belongs to a server.", ephemeral=True)
    if not _is_admin(interaction.user):
        return await interaction.response.send_message(REFUSAL, ephemeral=True)

    record = by_message(guild.id, interaction.message.id)
    if record is None:
        # The desk lost it: a restart with an unreadable file, or a card
        # older than the cap. Saying so is the point -- an administrator
        # who presses Approve and sees nothing happen will assume it did.
        return await interaction.response.send_message(
            "That request is no longer on file, so I cannot act on it. "
            "Nothing has been done; ask for it again.", ephemeral=True)
    if record["state"] != "pending":
        return await interaction.response.send_message(
            f"That one was already {record['state']}.", ephemeral=True)
    if _expired(record):
        record["state"] = "lapsed"
        _save(guild.id, record)
        await interaction.response.edit_message(content=_card(record), view=None)
        return

    record["signed_by"] = interaction.user.id
    record["signed_by_name"] = getattr(interaction.user, "display_name",
                                       str(interaction.user))
    record["signed_at"] = now().isoformat()

    if not approved:
        record["state"] = "denied"
        _save(guild.id, record)
        log.info(f"sign-off #{record['id']} denied by {record['signed_by_name']}")
        return await interaction.response.edit_message(
            content=_card(record), view=None)

    record["state"] = "approved"
    _save(guild.id, record)
    # Redrawn before the action rather than after it, so a slow ban does
    # not leave a card that still reads "waiting" while it is happening,
    # and a second administrator does not press the same button again.
    await interaction.response.edit_message(content=_card(record), view=None)
    record["outcome"] = await carry_out(guild, record)
    _save(guild.id, record)
    await _redraw(guild, record)


async def carry_out(guild, record):
    """Run the thing that was signed off, and say in one line how it went.

    Everything the request was checked against at filing time is checked
    again here, by the handler itself -- who may be touched, who is
    protected, what Discord will allow. This adds the one check that
    could not be made then: that the person who asked still stands where
    they stood.
    """
    executor = _executors.get(record["tool"])
    if executor is None:
        return "I no longer have a handler for that; nothing was done"

    invoker = guild.get_member(record["asked_by"]) if record.get("asked_by") \
        else None
    if invoker is None:
        return (f"{record['asked_by_name']} has left, so I did not act on "
                f"their word")
    keyed = _deps.get("in_cooperative")
    if keyed is not None:
        try:
            still = bool(keyed(invoker))
        except Exception as e:
            log.error(f"cooperative check failed at sign-off: {e!r}")
            still = False
        if not still:
            return (f"{record['asked_by_name']} is no longer in the "
                    f"cooperative, so I did not act on their word")

    try:
        result = await executor(guild, invoker, record["args"])
    except Exception as e:
        log.error(f"sign-off #{record['id']} failed: {e!r}")
        return f"it failed: {e!r}"
    try:
        parsed = json.loads(result)
    except (TypeError, ValueError):
        return str(result)[:300]
    if not isinstance(parsed, dict):
        return str(result)[:300]
    if parsed.get("error"):
        return f"Refused: {parsed['error']}"
    # What the handler says it did, not what the card said would be done.
    # They agree in the ordinary case; where they do not -- a purge that
    # found four messages rather than five -- the handler is the one that
    # was there.
    said = parsed.get("done") or "done"
    extra = ", ".join(f"{k}: {v}" for k, v in parsed.items()
                      if k not in ("done", "note") and v not in (None, "", []))
    return f"{str(said).capitalize()}{f' ({extra})' if extra else ''}"[:300]


def set_roll(in_cooperative):
    """The roll, for the one re-check that happens at the press."""
    _deps.update(in_cooperative=in_cooperative)


# ---------- what nobody signed ----------

async def sweep(guild):
    """Lapse the requests nobody got to. Called from the furniture loop,
    so a card left up overnight stops looking live."""
    if guild is None:
        return 0
    rows = _read(guild.id)
    stale = [r for r in rows if r["state"] == "pending" and _expired(r)]
    if not stale:
        return 0
    for record in stale:
        record["state"] = "lapsed"
    # Written before the cards are redrawn, not after. Redrawing awaits
    # the network, and an administrator pressing Approve in that gap
    # writes the file too -- so a sweep that saved afterwards would put
    # its own stale copy back over their decision.
    _write(guild.id, rows)
    for record in stale:
        await _redraw(guild, record, view=None)
    log.info(f"{len(stale)} sign-off request(s) lapsed in guild {guild.id}")
    return len(stale)


def summary(guild_id):
    """One line for the health card: what is waiting on somebody."""
    waiting = pending(guild_id)
    if not waiting:
        return None
    return (f"{len(waiting)} moderation request"
            f"{'' if len(waiting) == 1 else 's'} waiting on an administrator")
