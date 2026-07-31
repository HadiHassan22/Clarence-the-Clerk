"""The hands. Everything Eugene can do to the server itself.

warden.py holds the policy and can be argued with in a test; this holds
the buttons that are actually pressed, and every one of them is real. A
timeout here silences a person, a ban here removes them. So the shape of
this file is: resolve who is meant, check the three things that must be
checked, write the case down, then do it -- in that order, because a case
written after a failed action is a case nobody ever sees.

Two rules run through all of it.

The first is that the heavy ones are signed for. A warning, a timeout, a
kick, a ban, a swept channel and a locked room are asked for here and
carried out later, once an administrator has put their name to the
request at the desk in sanction.py. This reverses what this file used to
say, and the old rule was not silly: the confirmation had already
happened when the house decided who was in the cooperative, and a tool
that asks permission twice is a tool people route around. What changed
the answer is that these particular actions cannot be undone by saying
sorry, and that a signature from a second person is a different record
from a request by a first one. Everything the house did not name is
untouched and still immediate -- roles, announcements, settings, the
record, forgiveness -- because a gate on everything is a gate nobody
reads.

He still does not dither *at* anybody. There is no "are you sure" in the
conversation, no lecture about the seriousness of a timeout, no talking
the asker out of it. The asking is taken at face value and written up;
the pause happens on a card in the log room, not in the chat window.

The second is that the door is narrow. Only the cooperative reaches these
at all -- brain.py refuses everybody else a conversation, and
toolbox.dispatch refuses this tier a second time on its own, because one
gate is a bug away from no gate. Inside the door, three things are still
refused however politely they are asked: anyone standing above Eugene in
the role list, whose removal Discord would refuse anyway; the house's own
governance, so that removing a member of the cooperative stays a vote
rather than a word; and anything the person asking could not have done
with their own hands, which is the whole test for whether a bot has
quietly become a way to launder authority.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord

import bindings
import modules
import sanction
import warden

log = logging.getLogger("powers")

_deps = {}  # injected by clerk.py
_recent = defaultdict(lambda: deque(maxlen=30))  # (guild, user) -> timestamps

# Actions that reach a person rather than a message. Protected members are
# protected from exactly these.
HEAVY = ("timeout", "kick", "ban")


def configure(bot, in_cooperative, health_log=None):
    _deps.update(bot=bot, in_cooperative=in_cooperative, health_log=health_log)
    # The desk needs the same roll: a request signed off an hour later is
    # only carried out if whoever asked for it still stands where they
    # stood when they asked.
    sanction.set_roll(in_cooperative)


def _keyed(member):
    check = _deps.get("in_cooperative")
    return bool(check and member is not None and check(member))


def _err(text):
    return json.dumps({"error": text})


def _ok(**fields):
    return json.dumps(fields)


# ---------- working out who and what is meant ----------

def find_member(guild, needle):
    """A person, from however they were referred to in conversation: a
    mention, an id, a display name, a nickname, or enough of one to be
    unambiguous. Ambiguity is reported, never guessed at -- picking the
    wrong Sam and banning him is not a recoverable mistake."""
    text = str(needle or "").strip()
    if not text:
        return None, "nobody was named"
    digits = text.strip("<@!>&")
    if digits.isdigit():
        found = guild.get_member(int(digits))
        return (found, None) if found else (None, f"nobody here has the id {digits}")
    low = text.lower().lstrip("@")

    def names(member):
        # getattr rather than attribute access: this runs on the path to a
        # ban, and one member object missing a field it is supposed to have
        # must not take the whole command down with it.
        return [str(n).lower() for n in
                (getattr(member, "display_name", None), getattr(member, "name", None))
                if n]

    exact = [m for m in guild.members if low in names(m)]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, f"more than one person is called {text}; give me their id"
    near = [m for m in guild.members
            if any(n.startswith(low) for n in names(m))]
    if len(near) == 1:
        return near[0], None
    if len(near) > 1:
        names = ", ".join(m.display_name for m in near[:6])
        return None, f"that could be any of: {names}"
    return None, f"nobody here goes by {text}"


def find_channel(guild, needle, default=None):
    text = str(needle or "").strip()
    if not text:
        return default, None
    digits = text.strip("<#>")
    if digits.isdigit():
        found = guild.get_channel(int(digits))
        return (found, None) if found else (None, f"no channel with the id {digits}")
    low = text.lower().lstrip("#")
    for channel in guild.channels:
        if channel.name.lower() == low:
            return channel, None
    near = [c for c in guild.channels if low in c.name.lower()]
    if len(near) == 1:
        return near[0], None
    if len(near) > 1:
        return None, f"that could be any of: {', '.join('#' + c.name for c in near[:6])}"
    return None, f"there is no channel called {text}"


def find_role(guild, needle):
    text = str(needle or "").strip()
    digits = text.strip("<@&>")
    if digits.isdigit():
        found = guild.get_role(int(digits))
        return (found, None) if found else (None, f"no role with the id {digits}")
    low = text.lower().lstrip("@")
    for role in guild.roles:
        if role.name.lower() == low:
            return role, None
    near = [r for r in guild.roles if low in r.name.lower()]
    if len(near) == 1:
        return near[0], None
    if len(near) > 1:
        return None, f"that could be any of: {', '.join(r.name for r in near[:6])}"
    return None, f"there is no role called {text}"


# ---------- the three checks ----------

def _reachable(guild, target):
    """Whether Eugene outranks this person at all. Discord decides this and
    not us; asking first turns a raw Forbidden into a sentence a person can
    act on."""
    me = guild.me
    if me is None:
        return "I am not properly in this server yet"
    if target.id == me.id:
        return "I am not doing that to myself"
    if target.id == guild.owner_id:
        return "that is the server owner; Discord will not let me"
    if target.top_role >= me.top_role:
        return (f"{target.display_name} sits above me in the role list, so "
                f"Discord refuses. Move my role up if that is meant to change")
    return None


def _protected(guild, target, action):
    """The governance check. A member of the cooperative is not removed on
    one person's say-so, because their removal is a fundamental vote and a
    bot with a kick command is a way around it. The house may switch this
    off -- it is their rule, not mine -- and is told plainly what that
    means when they do."""
    if action not in HEAVY:
        return None
    if not warden.get(guild.id, "mod.protect_cooperative"):
        return None
    if not _keyed(target):
        return None
    return (f"{target.display_name} is in the cooperative, and removing one "
            f"of those is a vote, not a word. File it with propose_removal, "
            f"or turn off mod.protect_cooperative if the house means to")


def _vet(guild, invoker, target, action):
    if target.id == getattr(invoker, "id", None) and action in HEAVY:
        return "you cannot do that to yourself; ask someone else"
    return _reachable(guild, target) or _protected(guild, target, action)


# ---------- telling people ----------

async def _tell(guild, member, text):
    """The private word. Best effort by design: a closed DM must never
    stop the action, only go unremarked."""
    if not warden.get(guild.id, "mod.dm_on_action"):
        return False
    try:
        await member.send(text)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


def log_channel(guild):
    cid = warden.get(guild.id, "log.channel")
    return guild.get_channel(cid) if cid else bindings.channel(guild, "health")


async def journal(guild, text):
    """One line in the log room, if there is one. Never raises: the log is
    a courtesy and the action already happened."""
    if not modules.enabled(guild.id, "log"):
        return
    channel = log_channel(guild)
    if channel is None:
        return
    try:
        await channel.send(text[:1900])
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(f"could not write to the log room: {e!r}")


async def record(guild, kind, *, target, by, reason, detail=None, announce=True,
                 name=None):
    """Write the case, then say it happened. Returns the case."""
    case = warden.add_case(
        guild.id, kind,
        target_id=getattr(target, "id", None),
        target_name=name or getattr(target, "display_name", None)
        or getattr(target, "name", None) or str(target),
        moderator=getattr(by, "display_name", str(by)),
        reason=reason, detail=detail,
    )
    if announce and warden.get(guild.id, "log.mod"):
        line = (f"`#{case['case']}` **{kind}** — {case['target']} "
                f"by {case['moderator']} — {case['reason']}")
        await journal(guild, line)
    return case


# ---------- moderation ----------

ACTIONS = ("warn", "timeout", "untimeout", "kick", "ban", "unban", "nickname")

# What waits for a signature, and what does not. Lifting a punishment is
# on the free side of the line on purpose: an administrator's sign-off is
# a brake on harm, and making mercy wait an hour while the harm went
# through on somebody's word is the gate pointing the wrong way.
SIGNED_FOR = ("warn", "timeout", "kick", "ban")


async def act_moderate(guild, invoker, args):
    """What the model reaches. Either files the request for a signature or,
    for the light half of the list, does it now.

    The argument check happens before the filing rather than after the
    signature, so an administrator is never shown a card for something
    that was never going to work. A refusal an hour later, on a request
    nobody can now correct, is the worst of both designs.
    """
    action = str(args.get("action", "")).strip().lower()
    if action not in ACTIONS:
        return _err(f"action must be one of: {', '.join(ACTIONS)}")
    if action not in SIGNED_FOR:
        return await _run_moderate(guild, invoker, args)

    who = args.get("who")
    target, why = find_member(guild, who)
    if target is None:
        return _err(why)
    refusal = _vet(guild, invoker, target, action)
    if refusal:
        return _err(refusal)

    reason = str(args.get("reason") or "").strip()[:400]
    detail = f"Reason given: {reason}" if reason else "No reason given."
    if action == "timeout":
        # Clamped exactly as _timeout will clamp it. A card promising a
        # year and an action delivering 28 days is a card that lied to
        # the person who signed it.
        minutes = args.get("minutes")
        minutes = int(minutes) if str(minutes or "").strip().lstrip("-").isdigit() \
            else warden.get(guild.id, "mod.default_timeout_minutes")
        minutes = max(1, min(warden.TIMEOUT_MAX_MINUTES, minutes))
        head = f"Time out {target.display_name} for {minutes} minutes"
    elif action == "ban":
        days = max(0, min(7, int(args.get("delete_days") or 0)))
        head = f"Ban {target.display_name}"
        if days:
            detail += f" Clears {days} day(s) of their messages."
    else:
        head = f"{action.capitalize()} {target.display_name}"
    return await sanction.gate(guild, invoker, "moderate_member", args,
                               _run_moderate, head, detail)


async def _run_moderate(guild, invoker, args):
    action = str(args.get("action", "")).strip().lower()
    if action not in ACTIONS:
        return _err(f"action must be one of: {', '.join(ACTIONS)}")
    reason = str(args.get("reason") or "").strip()[:400]
    who = args.get("who")

    if action == "unban":  # the only one whose subject is not in the room
        return await _unban(guild, invoker, who, reason)

    target, why = find_member(guild, who)
    if target is None:
        return _err(why)
    refusal = _vet(guild, invoker, target, action)
    if refusal:
        return _err(refusal)

    stamped = f"{reason or 'no reason given'} (asked for by {invoker.display_name})"
    try:
        if action == "warn":
            return await _warn(guild, invoker, target, reason)
        if action == "timeout":
            return await _timeout(guild, invoker, target, args, stamped)
        if action == "untimeout":
            await target.timeout(None, reason=stamped)
            await record(guild, "untimeout", target=target, by=invoker,
                         reason=reason or "lifted early")
            return _ok(done="timeout lifted", who=target.display_name)
        if action == "nickname":
            new = str(args.get("nickname") or "").strip()[:32]
            before = target.display_name
            await target.edit(nick=new or None, reason=stamped)
            await record(guild, "nickname", target=target, by=invoker,
                         reason=reason or "renamed", detail=f"{before} -> {new or 'reset'}")
            return _ok(done="renamed", who=before, now=new or target.name)
        if action == "kick":
            await record(guild, "kick", target=target, by=invoker, reason=reason)
            await _tell(guild, target,
                        f"You have been removed from {guild.name}. Reason: "
                        f"{reason or 'none given'}.")
            await target.kick(reason=stamped)
            return _ok(done="kicked", who=target.display_name, reason=reason)
        if action == "ban":
            days = max(0, min(7, int(args.get("delete_days") or 0)))
            await record(guild, "ban", target=target, by=invoker, reason=reason,
                         detail=f"{days}d of messages cleared" if days else None)
            await _tell(guild, target,
                        f"You have been banned from {guild.name}. Reason: "
                        f"{reason or 'none given'}.")
            await guild.ban(target, reason=stamped,
                            delete_message_seconds=days * 86400)
            return _ok(done="banned", who=target.display_name, reason=reason,
                       cleared_days=days)
    except discord.Forbidden:
        return _err("Discord refused: I do not have that permission here")
    except discord.HTTPException as e:
        return _err(f"Discord refused: {e.text or e!r}")
    return _err("that action is not wired up")


async def _timeout(guild, invoker, target, args, stamped):
    minutes = args.get("minutes")
    minutes = int(minutes) if str(minutes or "").strip().lstrip("-").isdigit() else \
        warden.get(guild.id, "mod.default_timeout_minutes")
    minutes = max(1, min(warden.TIMEOUT_MAX_MINUTES, minutes))
    await target.timeout(timedelta(minutes=minutes), reason=stamped)
    reason = str(args.get("reason") or "").strip()
    await record(guild, "timeout", target=target, by=invoker, reason=reason,
                 detail=f"{minutes} minutes")
    await _tell(guild, target,
                f"You have been timed out in {guild.name} for {minutes} "
                f"minutes. Reason: {reason or 'none given'}.")
    return _ok(done="timed out", who=target.display_name, minutes=minutes,
               reason=reason)


async def _warn(guild, invoker, target, reason):
    """A warning is a case and nothing else, until enough of them add up.
    The adding up is the setting `warnings.timeout_at`, so a house that
    wants three strikes and a house that wants none both get what they
    asked for without either of them touching code."""
    case = await record(guild, "warn", target=target, by=invoker, reason=reason)
    live = warden.live_warnings(guild.id, target.id)
    await _tell(guild, target,
                f"Warning in {guild.name}: {reason or 'no reason given'}. "
                f"That is {len(live)} on your record.")
    ceiling = warden.get(guild.id, "warnings.timeout_at")
    escalated = None
    if ceiling and len(live) >= ceiling and _reachable(guild, target) is None:
        minutes = warden.get(guild.id, "warnings.timeout_minutes")
        try:
            await target.timeout(timedelta(minutes=minutes),
                                 reason=f"{len(live)} warnings")
            await record(guild, "timeout", target=target, by=guild.me,
                         reason=f"{len(live)} warnings on record",
                         detail=f"{minutes} minutes")
            escalated = f"{minutes} minute timeout, automatically"
        except (discord.Forbidden, discord.HTTPException):
            escalated = None
    return _ok(done="warned", who=target.display_name, case=case["case"],
               warnings_on_record=len(live), escalated=escalated)


async def _unban(guild, invoker, who, reason):
    text = str(who or "").strip()
    digits = text.strip("<@!>")
    user = None
    if digits.isdigit():
        user = discord.Object(id=int(digits))
        name = digits
    else:
        try:
            async for entry in guild.bans(limit=200):
                if text.lower() in (entry.user.name.lower(),
                                    str(entry.user).lower()):
                    user, name = entry.user, str(entry.user)
                    break
        except discord.Forbidden:
            return _err("I cannot read the ban list here")
    if user is None:
        return _err(f"{text} is not on the ban list")
    try:
        await guild.unban(user, reason=f"{reason} (asked for by {invoker.display_name})")
    except discord.NotFound:
        return _err(f"{text} is not banned")
    except discord.Forbidden:
        return _err("Discord refused: I cannot lift bans here")
    await record(guild, "unban", target=discord.Object(id=user.id), by=invoker,
                 reason=reason, name=name)
    return _ok(done="unbanned", who=name)


async def act_record(guild, invoker, args):
    """Someone's history, or the room's. Read-only and deliberately blunt:
    it is the same list the log room holds."""
    who = args.get("who")
    if who:
        target, why = find_member(guild, who)
        if target is None:
            return _err(why)
        live = warden.live_warnings(guild.id, target.id)
        return _ok(who=target.display_name,
                   live_warnings=len(live),
                   cases=warden.cases(guild.id, target_id=target.id, limit=15))
    return _ok(recent=warden.cases(guild.id, limit=15))


async def act_clear_warnings(guild, invoker, args):
    target, why = find_member(guild, args.get("who"))
    if target is None:
        return _err(why)
    count = warden.clear_warnings(guild.id, target.id)
    if count:
        await record(guild, "pardon", target=target, by=invoker,
                     reason=str(args.get("reason") or "wiped clean"),
                     detail=f"{count} cleared")
    return _ok(done="warnings cleared", who=target.display_name, cleared=count)


async def act_purge(guild, invoker, args):
    """A sweep, once somebody has signed for it. Deleted messages are the
    least recoverable thing in this file: a ban can be lifted and the
    person is still who they were, but a swept channel is gone."""
    channel, why = find_channel(guild, args.get("channel"))
    if channel is None:
        return _err(why or "which channel?")
    if not hasattr(channel, "purge"):
        return _err(f"#{channel.name} is not a channel I can sweep")
    count = max(1, min(warden.get(guild.id, "mod.purge_max"),
                       int(args.get("count") or 10)))
    whose = ""
    if args.get("from_member"):
        target, why = find_member(guild, args.get("from_member"))
        if target is None:
            return _err(why)
        whose = f" from {target.display_name}"
    reason = str(args.get("reason") or "").strip()
    return await sanction.gate(
        guild, invoker, "purge_messages", args, _run_purge,
        f"Delete {count} message(s){whose} in #{channel.name}",
        f"Reason given: {reason}" if reason else "No reason given.")


async def _run_purge(guild, invoker, args):
    """A sweep of recent messages. Capped by `mod.purge_max` and never
    silent: the log room gets a line saying who cleared what, because a
    room that quietly loses its history is how a place stops trusting the
    machinery."""
    channel, why = find_channel(guild, args.get("channel"))
    if channel is None:
        return _err(why or "which channel?")
    if not hasattr(channel, "purge"):
        return _err(f"#{channel.name} is not a channel I can sweep")
    ceiling = warden.get(guild.id, "mod.purge_max")
    count = max(1, min(ceiling, int(args.get("count") or 10)))
    check, scan = None, count
    whose = args.get("from_member")
    if whose:
        target, why = find_member(guild, whose)
        if target is None:
            return _err(why)
        # "Delete Sam's last five" means five of Sam's, so the sweep has to
        # look past other people's messages to find them -- and then stop
        # dead on the fifth. Counting the matches rather than trimming the
        # result afterwards is the whole of it: a check that keeps saying
        # yes deletes everything it is shown, and what it is shown is a
        # multiple of what was asked for.
        scan = min(count * 20, 1000)
        taken = []

        def check(message, _target=target, _taken=taken):
            if len(_taken) >= count or message.author.id != _target.id:
                return False
            _taken.append(message.id)
            return True

    try:
        gone = await channel.purge(
            limit=scan, check=check,
            reason=f"asked for by {invoker.display_name}")
    except discord.Forbidden:
        return _err(f"I cannot delete messages in #{channel.name}")
    except discord.HTTPException as e:
        return _err(f"Discord refused: {e.text or e!r}")
    await record(guild, "purge", target=channel, by=invoker,
                 reason=str(args.get("reason") or "swept"),
                 detail=f"{len(gone)} messages in #{channel.name}")
    return _ok(done="swept", channel=channel.name, deleted=len(gone),
               note="messages older than a fortnight cannot be swept by anyone")


async def act_channel(guild, invoker, args):
    action = str(args.get("action") or "").strip().lower()
    channel, why = find_channel(guild, args.get("channel"))
    if channel is None:
        return _err(why or "which channel?")
    if action == "slowmode":
        seconds = max(0, min(21600, int(args.get("seconds") or 0)))
        head = (f"Slow #{channel.name} to one message every {seconds}s"
                if seconds else f"Turn slowmode off in #{channel.name}")
    elif action in ("lock", "unlock"):
        head = f"{action.capitalize()} #{channel.name}"
    else:
        return _err("action must be slowmode, lock or unlock")
    reason = str(args.get("reason") or "").strip()
    return await sanction.gate(
        guild, invoker, "channel_control", args, _run_channel, head,
        f"Reason given: {reason}" if reason else "No reason given.")


async def _run_channel(guild, invoker, args):
    action = str(args.get("action") or "").strip().lower()
    channel, why = find_channel(guild, args.get("channel"))
    if channel is None:
        return _err(why or "which channel?")
    try:
        if action == "slowmode":
            seconds = max(0, min(21600, int(args.get("seconds") or 0)))
            await channel.edit(slowmode_delay=seconds,
                               reason=f"asked for by {invoker.display_name}")
            await record(guild, "slowmode", target=channel, by=invoker,
                         reason=str(args.get("reason") or ""),
                         detail=f"#{channel.name} -> {seconds}s")
            return _ok(done="slowmode set", channel=channel.name, seconds=seconds)
        if action in ("lock", "unlock"):
            everyone = guild.default_role
            overwrite = channel.overwrites_for(everyone)
            overwrite.send_messages = False if action == "lock" else None
            await channel.set_permissions(
                everyone, overwrite=overwrite,
                reason=f"asked for by {invoker.display_name}")
            await record(guild, action, target=channel, by=invoker,
                         reason=str(args.get("reason") or ""),
                         detail=f"#{channel.name}")
            return _ok(done=f"{action}ed", channel=channel.name)
    except discord.Forbidden:
        return _err(f"I do not have permission to change #{channel.name}")
    except discord.HTTPException as e:
        return _err(f"Discord refused: {e.text or e!r}")
    return _err("action must be slowmode, lock or unlock")


async def act_assign_role(guild, invoker, args):
    """Any role, on or off anybody -- the elevated cousin of the colour
    tools, which only ever touch the person asking. Two roles are refused
    outright: the cooperative and the member role, because those two decide
    who votes and who is in, and handing either out in a chat window would
    make the roster something a conversation can rewrite."""
    target, why = find_member(guild, args.get("who"))
    if target is None:
        return _err(why)
    role, why = find_role(guild, args.get("role"))
    if role is None:
        return _err(why)
    for key in ("cooperative", "member"):
        bound = bindings.role(guild, key)
        if bound is not None and bound.id == role.id:
            return _err(f"{role.name} decides who has a vote here, so it is "
                        f"given by the house and not by me. That is an "
                        f"invitation vote, or /setup for the first one")
    me = guild.me
    if me is None or role >= me.top_role:
        return _err(f"{role.name} sits at or above my own role, so Discord "
                    f"will not let me hand it out")
    on = args.get("on")
    on = True if on is None else bool(on)
    try:
        if on:
            await target.add_roles(role, reason=f"asked for by {invoker.display_name}")
        else:
            await target.remove_roles(role, reason=f"asked for by {invoker.display_name}")
    except discord.Forbidden:
        return _err("Discord refused: I cannot manage that role")
    await record(guild, "role", target=target, by=invoker,
                 reason=str(args.get("reason") or ""),
                 detail=f"{'+' if on else '-'}{role.name}", announce=False)
    return _ok(done="role given" if on else "role taken", who=target.display_name,
               role=role.name)


async def act_announce(guild, invoker, args):
    """Say something in a room, as Eugene, on somebody's behalf. Attributed
    in the log, because an announcement that appears to come from the
    machinery should still be traceable to a person."""
    channel, why = find_channel(guild, args.get("channel"))
    if channel is None:
        return _err(why or "which channel?")
    text = str(args.get("text") or "").strip()
    if not text:
        return _err("nothing to say")
    if not hasattr(channel, "send"):
        return _err(f"{channel.name} is not a channel I can post in")
    mention_everyone = bool(args.get("ping_everyone"))
    allowed = discord.AllowedMentions(everyone=mention_everyone, roles=False,
                                      users=True)
    try:
        sent = await channel.send(text[:1900], allowed_mentions=allowed)
    except discord.Forbidden:
        return _err(f"I cannot post in #{channel.name}")
    await journal(guild, f"📣 announcement in #{channel.name} for "
                         f"{invoker.display_name}")
    return _ok(done="posted", channel=channel.name, link=sent.jump_url)


# ---------- settings, changed by talking ----------

NAMED = {"channel", "role"}


async def act_settings(guild, invoker, args):
    """What can be set, what it is set to, and what each one means.

    Named group: everything about it, help included. No group: the values
    alone, across all of them. That split is not tidiness -- the full table
    with its help runs to several thousand characters, and a model that
    pays for it every time somebody asks whether welcomes are on will
    quietly stop asking.
    """
    group = str(args.get("group") or "").strip().lower() or None
    if group and group not in warden.GROUPS:
        return _err(f"groups are: {', '.join(warden.GROUPS)}")
    now = warden.config(guild.id)

    def shown(key, value):
        kind = warden.SPEC[key]["type"]
        if kind not in NAMED or not value:
            return value
        thing = (guild.get_channel(value) if kind == "channel"
                 else guild.get_role(value))
        if thing is None:
            return f"{value} (gone)"
        return f"#{thing.name}" if kind == "channel" else thing.name

    if group is None:
        return _ok(
            values={key: shown(key, value) for key, value in now.items()},
            chosen_by_this_house=list(warden.overrides(guild.id)),
            groups=list(warden.GROUPS),
            note="call this again with a group for what each one means",
        )
    out = {}
    for key, spec in warden.describe(group).items():
        out[key] = {"now": shown(key, now.get(key)), **spec}
    return _ok(settings=out, chosen_by_this_house=[
        k for k in warden.overrides(guild.id) if k.startswith(f"{group}.")])


async def act_set_setting(guild, invoker, args):
    """Change one. This is the tool that makes the rest of the file
    configurable by conversation, so it is also the one with the most
    careful reply: what it was, what it is, and -- when the change turns
    something dangerous on or a protection off -- what that now means."""
    key = str(args.get("key") or "").strip()
    spec = warden.SPEC.get(key)
    if spec is None:
        near = [k for k in warden.SPEC if key.lower() in k][:8]
        return _err(f"'{key}' is not a setting." +
                    (f" Did you mean: {', '.join(near)}?" if near else
                     " Call list_settings."))
    raw = args.get("value")
    before = warden.get(guild.id, key)

    # A person says "the general channel"; the store keeps an id. Resolving
    # here rather than in warden.py is what lets the settings survive a
    # rename: only the number is ever written down.
    if spec["type"] in NAMED and raw not in (None, "", "none", "off"):
        finder = find_channel if spec["type"] == "channel" else find_role
        thing, why = finder(guild, raw)
        if thing is None:
            return _err(why)
        raw = thing.id
    if isinstance(raw, str) and raw.strip().lower() in ("none", "unset", "default"):
        raw = None

    ok, held = warden.set_value(guild.id, key, raw)
    if not ok:
        return _err(held)

    shown = held
    if spec["type"] in NAMED and held:
        thing = (guild.get_channel(held) if spec["type"] == "channel"
                 else guild.get_role(held))
        shown = getattr(thing, "name", held)
    warning = None
    if key == "mod.protect_cooperative" and held is False:
        warning = ("that is off now: any one of you can have another member "
                   "removed with a sentence, no ballot")
    if key == "automod.exempt_cooperative" and held is False:
        warning = "the filters now apply to the cooperative too, you included"
    await journal(guild, f"⚙️ {invoker.display_name} set `{key}` to `{shown}`")
    return _ok(done="set", key=key, was=before, now=shown, warning=warning)


async def act_reset_settings(guild, invoker, args):
    group = str(args.get("group") or "").strip().lower()
    if group and group not in warden.GROUPS:
        return _err(f"groups are: {', '.join(warden.GROUPS)}")
    dropped = warden.reset(guild.id, f"{group}." if group else None)
    await journal(guild, f"⚙️ {invoker.display_name} reset "
                         f"{group or 'every setting'} to the defaults")
    return _ok(done="reset", group=group or "all", keys_cleared=dropped)


# ---------- the shelf ----------

async def act_tag(guild, invoker, args):
    """Save, recall, or drop a stock answer. The rules link, the server
    address, the thing three people ask a week -- written once by whoever
    knows it, repeated by Eugene forever after."""
    name = str(args.get("name") or "").strip().lower()
    action = str(args.get("action") or "recall").strip().lower()
    if action == "list" or (not name and action == "recall"):
        shelf = warden.tags(guild.id)
        return _ok(tags={k: v["content"][:80] for k, v in shelf.items()})
    if not name:
        return _err("which tag?")
    if action == "save":
        content = str(args.get("content") or "").strip()
        if not content:
            return _err("a tag needs something in it")
        ok, held = warden.set_tag(guild.id, name, content, invoker.display_name)
        return _ok(done="saved", tag=held) if ok else _err(held)
    if action == "drop":
        return (_ok(done="dropped", tag=name) if warden.drop_tag(guild.id, name)
                else _err(f"there is no tag called {name}"))
    found = warden.get_tag(guild.id, name)
    if found is None:
        shelf = ", ".join(list(warden.tags(guild.id))[:10])
        return _err(f"no tag called {name}." + (f" There is: {shelf}" if shelf else ""))
    return _ok(tag=name, content=found["content"], by=found["by"], at=found["at"])


# ---------- the master switches ----------
# The feature list, from the other side of the conversation. `set_setting`
# tunes a feature; this is what switches one on, and it exists because
# taking the `<group>.enabled` keys out of the settings table would
# otherwise have taken "turn the filters on" out of the language with them.

async def act_list_features(guild, invoker, args):
    rows = []
    for key in modules.keys():
        spec = modules.spec(key)
        rows.append({
            "feature": key,
            "name": spec["name"],
            "on": modules.enabled(guild.id, key),
            "what": spec["blurb"],
        })
    return _ok(features=rows,
               note="on=false means it does nothing at all and its settings "
                    "are read by nobody")


async def act_set_feature(guild, invoker, args):
    key = str(args.get("feature") or "").strip().lower()
    if key not in modules.SPEC:
        return _err(f"there is no {key!r} feature; they are: "
                    + ", ".join(modules.keys()))
    on = args.get("on")
    if isinstance(on, str):
        on = on.strip().lower() in warden.TRUE_WORDS
    was = modules.enabled(guild.id, key)
    changed, knock_on = modules.set_enabled(guild.id, key, bool(on))
    name = modules.name(key)
    if not changed and was == bool(on):
        return _ok(feature=key, on=was, note=f"{name} was already "
                   + ("on" if was else "off"))
    await journal(
        guild,
        f"⚙️ {name} switched {'on' if on else 'off'} by "
        f"{getattr(invoker, 'display_name', 'someone')}.",
    )
    log.info(f"guild {guild.id}: {key} -> {'on' if on else 'off'} "
             f"by {getattr(invoker, 'display_name', '?')}")
    result = _ok(feature=key, on=modules.enabled(guild.id, key))
    if knock_on:
        result = _ok(
            feature=key, on=modules.enabled(guild.id, key),
            also=[modules.name(k) for k in knock_on],
            note=("switched on with it, because " + name + " stands on it")
            if on else
            (", ".join(modules.name(k) for k in knock_on)
             + " went with it, because they stand on " + name),
        )
    # A feature can be on and still not running, and the difference is the
    # whole point of saying it out loud.
    waiting = modules.blockers(
        guild.id, key,
        rooms={r for r in modules.spec(key)["rooms"]
               if bindings.channel(guild, r) is not None},
        roles={r for r in modules.spec(key)["roles"]
               if bindings.role(guild, r) is not None},
        brain=True,
    )
    if on and waiting:
        return _ok(feature=key, on=True, but="; ".join(waiting),
                   how="/setup fixes that")
    return result


ACTIONS_TABLE = {
    "set_feature": act_set_feature,
    "list_features": act_list_features,
    "moderate_member": act_moderate,
    "member_record": act_record,
    "clear_warnings": act_clear_warnings,
    "purge_messages": act_purge,
    "channel_control": act_channel,
    "assign_role": act_assign_role,
    "announce": act_announce,
    "list_settings": act_settings,
    "set_setting": act_set_setting,
    "reset_settings": act_reset_settings,
    "tag": act_tag,
}

# The desk holds names, not functions, so that a card filed before a
# deploy still reaches a handler after it. Registered at import rather
# than in configure(), because a card can be pressed before this process
# has ever been told who is in the cooperative -- and the press is what
# looks the handler up.
sanction.register("moderate_member", _run_moderate)
sanction.register("purge_messages", _run_purge)
sanction.register("channel_control", _run_channel)


# ---------- what happens without anyone asking ----------

def _welcome_room(guild):
    """Where a greeting goes: the bound welcome room, and nowhere else.

    It used to guess -- the channel a setting named, else the bound room,
    else Discord's system channel, else the first channel with "general" in
    its name -- which is the exact name-matching this codebase spent an
    inversion getting rid of, and it had a worse consequence than
    untidiness. A server that already greets people, with Discord's own
    join notices or with a bot it had before this one, got greeted twice
    the moment the clerk arrived, in a room nobody had pointed him at.

    So: no binding, no greeting. The feature reads as dormant, the panel
    says which room it is waiting for, and a house that has something else
    for this simply never points him at one. A bot that speaks in a room
    nobody chose is the whole failure this project is against.
    """
    return bindings.channel(guild, "welcome")


def _fill(member):
    guild = member.guild
    return dict(mention=member.mention, user=member.display_name,
                server=guild.name, count=guild.member_count)


async def on_join(member):
    """Greeting, private note, and the arrival role."""
    guild = member.guild
    if not modules.enabled(guild.id, "welcome"):
        return
    cfg = warden.config(guild.id)
    channel = _welcome_room(guild)
    if channel is not None:
        try:
            await channel.send(warden.render(cfg["welcome.message"],
                                             **_fill(member)))
        except (discord.Forbidden, discord.HTTPException):
            log.warning("could not post a welcome")
    if cfg["welcome.dm"]:
        try:
            await member.send(warden.render(cfg["welcome.dm"], **_fill(member)))
        except (discord.Forbidden, discord.HTTPException):
            pass
    rid = cfg["welcome.role"]
    role = guild.get_role(rid) if rid else None
    if role is not None and guild.me is not None and role < guild.me.top_role:
        try:
            await member.add_roles(role, reason="the arrival role")
        except (discord.Forbidden, discord.HTTPException):
            log.warning(f"could not put {role.name} on an arrival")


async def on_leave(member):
    guild = member.guild
    if not modules.enabled(guild.id, "welcome"):
        return
    cfg = warden.config(guild.id)
    if not cfg["goodbye.enabled"]:
        return
    channel = _welcome_room(guild)
    if channel is not None:
        try:
            await channel.send(warden.render(cfg["goodbye.message"],
                                             **_fill(member)))
        except (discord.Forbidden, discord.HTTPException):
            pass


# Writing an arrival down is the audit log's job and not the greeting's.
# It lived inside `on_join` behind the Arrivals gate, which meant switching
# off the public hello also switched off the private record of who came and
# went -- two unrelated things on one switch, and the one that got lost was
# the one somebody would go looking for after the fact.

async def on_join_logged(member):
    if warden.get(member.guild.id, "log.joins"):
        await journal(member.guild,
                      f"📥 {member.display_name} arrived "
                      f"({member.guild.member_count} here now)")


async def on_leave_logged(member):
    if warden.get(member.guild.id, "log.joins"):
        await journal(member.guild, f"📤 {member.display_name} left")


async def on_message(message):
    """The filters. Returns True when the message was deleted, so the
    caller knows not to go on treating it as something to reply to."""
    guild = message.guild
    if guild is None or message.author.bot:
        return False
    return await _automod(message, warden.config(guild.id))


async def _automod(message, cfg):
    if not modules.enabled(message.guild.id, "moderation"):
        return False
    author = message.author
    if cfg["automod.exempt_cooperative"] and _keyed(author):
        return False
    if str(message.channel.id) in [str(c) for c in cfg["automod.exempt_channels"]]:
        return False
    if author.guild_permissions.manage_messages:
        return False  # anyone who could undo it anyway
    stamp = time.time()
    trail = _recent[(message.guild.id, author.id)]
    trail.append(stamp)
    hits = warden.scan(cfg, message.content or "",
                       mention_count=len(warden.MENTION.findall(message.content or "")),
                       recent=list(trail))
    ruling = warden.verdict(hits)
    if ruling is None:
        return False

    guild = message.guild
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass
    if ruling["action"] == "warn":
        await _warn(guild, guild.me, author, f"automod: {ruling['reason']}")
    elif ruling["action"] == "timeout" and _reachable(guild, author) is None:
        minutes = cfg["automod.timeout_minutes"]
        try:
            await author.timeout(timedelta(minutes=minutes),
                                 reason=f"automod: {ruling['reason']}")
            await record(guild, "timeout", target=author, by=guild.me,
                         reason=f"automod: {ruling['reason']}",
                         detail=f"{minutes} minutes")
            await _tell(guild, author,
                        f"Timed out in {guild.name} for {minutes} minutes: "
                        f"{ruling['reason']}.")
        except (discord.Forbidden, discord.HTTPException):
            pass
    else:
        await record(guild, "deleted", target=author, by=guild.me,
                     reason=f"automod: {ruling['reason']}")
    return True


async def on_delete(message):
    guild = message.guild
    if guild is None or message.author.bot:
        return
    if not warden.get(guild.id, "log.deletes"):
        return
    body = (message.content or "")[:400] or "(no text)"
    await journal(guild, f"🗑️ {message.author.display_name} in "
                         f"#{message.channel.name}: {body}")


async def on_edit(before, after):
    guild = after.guild
    if guild is None or after.author.bot or before.content == after.content:
        return
    if not warden.get(guild.id, "log.edits"):
        return
    await journal(guild, f"✏️ {after.author.display_name} in "
                         f"#{after.channel.name}\nbefore: "
                         f"{(before.content or '')[:300]}\nafter: "
                         f"{(after.content or '')[:300]}")


def summary(guild):
    """One screen of how the features that are on are set, for the setup
    card. Whether each one is on at all is the feature list's answer, not
    this one's, and saying it twice is what let the two disagree."""
    cfg = warden.config(guild.id)
    def mark(on):
        return "✅" if on else "⬜"
    return "\n".join([
        f"{mark(cfg['goodbye.enabled'])} **goodbye** — announces departures "
        f"as well as arrivals",
        f"{mark(cfg['automod.exempt_cooperative'])} **automod** — "
        f"{'exempts' if cfg['automod.exempt_cooperative'] else 'includes'} "
        f"the cooperative",
        f"{mark(bool(cfg['log.channel']))} **log** — where actions are written",
        f"{mark(cfg['mod.protect_cooperative'])} **protect_cooperative** — "
        f"removing a member needs a vote",
    ])
