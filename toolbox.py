"""The harness: the only door between Eugene's brain and reality.

A frozen registry of typed tools. The model may request these and nothing
else; every dispatch is validated, executed by hand-written handlers, and
logged. Stage 2 ships minor (read-only) tools; major act-gated tools come
with the executor stage.

Anonymity firewall: get_bill and get_act strip ballots, anonymous-note
authorship, and sealed tallies before the model ever sees them.
"""

import asyncio
import copy
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import modules

log = logging.getLogger("toolbox")

# configured by clerk.py at startup
_paths = {}
_actions = {}
_log_lock = asyncio.Lock()

PEOPLE_KINDS = ("invite", "kick")


def configure(here: Path, data: Path, actions=None, in_cooperative=None,
              numbers=None):
    """actions: hand-written handlers injected by clerk.py, so the harness
    never reaches into the bot's internals itself.

    in_cooperative decides who a vote is counted against. Without it the
    roster is simply left out of what `server_info` reports, rather than
    guessed at -- a wrong threshold is worse than a missing one.

    numbers(guild) is the same bargain for the figures those thresholds are
    worked out from: given, the report quotes the house's own; withheld, it
    quotes the defaults rather than inventing anything."""
    _paths["here"] = here
    _paths["data"] = data
    _paths["log"] = data / "logs" / "executor_log.json"
    _paths["log"].parent.mkdir(parents=True, exist_ok=True)
    _adopt_legacy_log(data)
    _paths["in_cooperative"] = in_cooperative
    _paths["numbers"] = numbers
    _actions.update(actions or {})


def _adopt_legacy_log(data: Path):
    """The audit log used to sit at the top of the data directory, beside
    the record. It is not the record -- it is a log -- so it has moved in
    with the rest of them. Anything already written comes along, once,
    rather than being stranded next to a file that stopped growing."""
    old = data / "executor_log.json"
    if old.exists() and not _paths["log"].exists():
        try:
            os.replace(old, _paths["log"])
        except OSError as e:
            log.warning(f"could not move the audit log into logs/: {e!r}")
            _paths["log"] = old


def _atomic(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _load(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


async def _audit(entry):
    async with _log_lock:
        entries = _load(_paths["log"], [])
        entries.append(entry)
        _atomic(_paths["log"], entries)


# ---------- handlers (all read-only in stage 2) ----------

async def _get_standing_orders(guild, invoker, args):
    return (_paths["here"] / "standing-orders.md").read_text()


async def _list_acts(guild, invoker, args):
    limit = min(int(args.get("limit", 50)), 50)
    acts = _load(_paths["data"] / "acts.json", [])
    index = [
        {"act": a["act"], "title": a["title"], "author": a["author"],
         "passed_at": a["passed_at"]}
        for a in acts[-limit:]
    ]
    return json.dumps(index)


def _bill_kind(bill_no):
    for b in _load(_paths["data"] / "bills.json", []):
        if b["no"] == bill_no:
            return b.get("kind", "ordinary")
    return "ordinary"


async def _get_act(guild, invoker, args):
    act_no = int(args["act_no"])
    for a in _load(_paths["data"] / "acts.json", []):
        if a["act"] == act_no:
            a = copy.deepcopy(a)
            if _bill_kind(a.get("bill")) in PEOPLE_KINDS:
                a.pop("tally", None)  # sealed, and stays sealed
            return json.dumps(a)
    return json.dumps({"error": f"no Decision {act_no} on record"})


async def _list_bills(guild, invoker, args):
    status = args.get("status", "all")
    bills = _load(_paths["data"] / "bills.json", [])
    index = [
        {"no": b["no"], "title": b["title"], "kind": b.get("kind", "ordinary"),
         "status": b["status"], "author": b["author"]}
        for b in bills
        if status in ("all", None) or b["status"] == status
    ]
    return json.dumps(index[-50:])


async def _get_bill(guild, invoker, args):
    bill_no = int(args["bill_no"])
    for b in _load(_paths["data"] / "bills.json", []):
        if b["no"] == bill_no:
            b = copy.deepcopy(b)
            b.pop("ballots", None)  # individual votes: never
            if b.get("kind") in PEOPLE_KINDS:
                b.pop("tally", None)
                b.pop("tally_line", None)
                b.pop("invite_url", None)
            notes = []
            for slots in b.pop("notes", {}).values():
                for kind, note in slots.items():
                    notes.append(
                        {
                            "author": "anonymous" if kind == "anon" else note.get("display", "?"),
                            "text": note.get("text", ""),
                            "first_at": note.get("first_at"),
                        }
                    )
            b["notes"] = sorted(notes, key=lambda n: n.get("first_at") or "")
            return json.dumps(b)
    return json.dumps({"error": f"no Proposal No. {bill_no} on record"})


async def _server_info(guild, invoker, args):
    cats = []
    for cat in guild.categories:
        cats.append(
            {
                "category": cat.name,
                "channels": [
                    {"name": c.name, "type": c.type.name,
                     "topic": getattr(c, "topic", None)}
                    for c in cat.channels
                ],
            }
        )
    top = [
        {"name": c.name, "type": c.type.name, "topic": getattr(c, "topic", None)}
        for c in guild.channels
        if c.category is None and not hasattr(c, "channels")
    ]
    info = {
        "server": guild.name,
        "members": guild.member_count,
        "top_level": top,
        "categories": cats,
    }
    # member_count above is everybody in the building, bots included. What a
    # vote is counted against is a different and smaller number, and it is
    # the one that decides what carries -- so it is worked out here rather
    # than left for the model to infer from the other.
    keyed = _paths.get("in_cooperative")
    if keyed is not None:
        import roster

        figures = _paths.get("numbers")
        held = figures(guild) if figures is not None else {}
        away_days = held.get("away_days", roster.AUTO_AWAY_DAYS)
        share = held.get("fundamental_share")
        size = len(roster.active(guild, keyed, away_days=away_days))
        info["roster"] = {
            "counted": size,
            "away": sum(
                1 for m in guild.members
                if not m.bot and keyed(m) and roster.is_away(m, away_days)
            ),
            "normal_needs": roster.required(size, "normal", share),
            "fundamental_needs": roster.required(size, "fundamental", share),
            "note": "counted now, from who is here; thresholds are shares "
                    "of this, never fixed numbers. This is the cooperative's "
                    "roster: a poll open to the whole server is carried by a "
                    "majority of whoever votes, once quorum is met.",
        }
        if held:
            info["voting_numbers"] = held
    return json.dumps(info)


# ---------- registry ----------

REGISTRY = {
    "get_standing_orders": {
        "tier": "minor",
        "description": "The rules of procedure, full text: tiers, thresholds, "
        "how votes close, and what is actually wired up so far.",
        "parameters": {"type": "object", "properties": {}},
        "handler": _get_standing_orders,
    },
    "list_acts": {
        "tier": "minor",
        "description": "Index of decisions on the record: number, title, "
        "author, date.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "max entries, up to 50"}},
        },
        "handler": _list_acts,
    },
    "get_act": {
        "tier": "minor",
        "description": "Full text and details of one decision on the record, "
        "by number.",
        "parameters": {
            "type": "object",
            "properties": {"act_no": {"type": "integer"}},
            "required": ["act_no"],
        },
        "handler": _get_act,
    },
    "list_bills": {
        "tier": "minor",
        "description": "Index of proposals: number, title, kind, status, "
        "author. Status 'on_floor' means still open for votes.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["on_floor", "passed", "failed", "all"],
                }
            },
        },
        "handler": _list_bills,
    },
    "get_bill": {
        "tier": "minor",
        "description": "Full details of one proposal: what, why, status, "
        "notes. Individual ballots are sealed and never available.",
        "parameters": {
            "type": "object",
            "properties": {"bill_no": {"type": "integer"}},
            "required": ["bill_no"],
        },
        "handler": _get_bill,
    },
    "server_info": {
        "tier": "minor",
        "description": "The server's structure: categories, channels, topics, member count.",
        "parameters": {"type": "object", "properties": {}},
        "handler": _server_info,
    },
}


# The colour limits are the house's, not ours, and they are written into
# these descriptions rather than stated in them: "five at most" was hard
# coded here while the shipped cap was one, so a single request carried
# both numbers -- the true one in the system prompt, the false one here.
# The model believed the tool, offered a second role, and was refused by
# its own harness in front of a member. Anything below that quotes a limit
# uses a {placeholder} and is filled in by `declarations` from the same
# `settings.voting` every other reader of these numbers uses.
COLOUR_TOOLS = {
    "list_color_roles": {
        "description": "The wardrobe and where the person asking stands in "
        "it: every colour role by name and colour, who made each one, how "
        "many wear it, and -- separately -- which ones they made, which ones "
        "they are wearing, and whether they have room for another. Call this "
        "before any other colour tool unless they named a role you have "
        "already seen spelled out this turn: guessing a role's name is how "
        "you end up telling somebody a role they own belongs to a stranger.",
        "parameters": {"type": "object", "properties": {}},
    },
    "create_color_role": {
        "description": "Create a colour role, worn by the person you are "
        "talking to or by anybody they name. This house lets one person make "
        "{make} of their own, so check what they already have before offering "
        "a new one -- if they are at the limit, recolouring what they have is "
        "what they want, not a deletion. Made for somebody else it still "
        "counts against the asker, because the role stays theirs to rename "
        "and delete. Purely cosmetic: a name and a colour. Refused if a role "
        "by that name already exists, so pass what they actually asked for "
        "rather than a variation on it.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "color": {
                    "type": "string",
                    "description": "a colour name like 'sea green' or 'hot "
                    "pink', or hex like #ff9d2e. Pass on whatever word they "
                    "used; do not convert it to hex yourself.",
                },
                "member": {
                    "type": "string",
                    "description": "who ends up wearing it -- a mention, an "
                    "id, or the name they used. Omit for the person asking.",
                },
            },
            "required": ["name", "color"],
        },
    },
    "edit_color_role": {
        "description": "Rename or recolour a role the person you are talking "
        "to created. Only its creator may change it. This is the right tool "
        "when somebody at their limit wants a different colour. If they are "
        "not wearing it, it goes back on them so the colour actually shows.",
        "parameters": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "the role's current name"},
                "name": {"type": "string"},
                "color": {
                    "type": "string",
                    "description": "a colour name like 'sea green', or hex",
                },
            },
            "required": ["role"],
        },
    },
    "delete_color_role": {
        "description": "Delete a colour role the person you are talking to "
        "created. Only its creator may delete it.",
        "parameters": {
            "type": "object",
            "properties": {"role": {"type": "string"}},
            "required": ["role"],
        },
    },
    "wear_color_role": {
        "description": "Put an existing colour role on the person you are "
        "talking to, or on anybody they name. Anyone may put any colour on "
        "anyone; {wear} at a time in this house. Owning a colour and "
        "wearing it are different things -- somebody can have made one they "
        "took off.",
        "parameters": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "member": {
                    "type": "string",
                    "description": "who wears it -- a mention, an id, or the "
                    "name they used. Omit for the person asking.",
                },
            },
            "required": ["role"],
        },
    },
    "shed_color_role": {
        "description": "Take a colour role off someone: off the person "
        "asking always, and off anybody else only if they made that role. "
        "Nothing is destroyed: the role still exists and can go back on.",
        "parameters": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "member": {
                    "type": "string",
                    "description": "whose colour comes off -- a mention, an "
                    "id, or the name they used. Omit for the asker.",
                },
            },
            "required": ["role"],
        },
    },
}

BILL_TOOLS = {
    "propose_bill": {
        "description": "File a proposal for the person you are talking to, in "
        "their name. Use it the moment they say they want something changed: "
        "draft the what and the why from what they said, file it, and tell "
        "them the number. Do not ask them to confirm and do not send them to "
        "the button. Filing decides nothing; the cooperative votes and you "
        "have no say in it.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "a short name for the law"},
                "what": {"type": "string", "description": "the operative text: what becomes law if it passes"},
                "why": {"type": "string", "description": "their reasons, in their voice. A bill without reasons is not a bill"},
                "priority": {"type": "boolean", "description": "true only if they ask you to chase people about it. It direct-messages everyone who has not voted, so it is for the ones that matter, not the ones they are keen on"},
            },
            "required": ["title", "what", "why"],
        },
    },
    "open_poll": {
        "description": "Ask the whole server a question, in the name of the "
        "person you are talking to. This is the other kind of vote and the "
        "difference is who it is put to: a proposal is the cooperative's "
        "business, a poll is everybody's. Use it when they want to know what "
        "the room thinks rather than to change something. It is carried by a "
        "majority of whoever votes once enough of them have, so nobody is "
        "chased about it and not voting costs nothing. Options are for a "
        "choice of answers; leave them out for yes/no.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "a short name for the question"},
                "what": {"type": "string", "description": "the question itself, as the room will read it"},
                "why": {"type": "string", "description": "why they are asking, in their voice"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2 to 10 answers for a choice ballot, or omit entirely for yes/no",
                },
            },
            "required": ["title", "what", "why"],
        },
    },
    "close_floor": {
        "description": "Call time on a vote that is still open: count the "
        "ballots, rule it passed or failed, and do everything a close "
        "normally does. Use it when someone asks you to close a vote. It "
        "returns the ruling, the tally, what you have already done, and "
        "what is left for human hands: report all of it, and be clear that "
        "a decision on the record is not the same as a thing that has "
        "happened. You cannot see the tally until it shuts, so this is never "
        "a way to end a vote at a convenient moment.",
        "parameters": {
            "type": "object",
            "properties": {
                "bill_no": {"type": "integer", "description": "the bill's number; call list_bills if unsure"},
            },
            "required": ["bill_no"],
        },
    },
    "propose_removal": {
        "description": "Propose that a member of the cooperative be removed, "
        "and open the ballot. This is the only route: you cannot kick one of "
        "them yourself and nobody can, because removal is a fundamental vote. "
        "The subject cannot vote and keeps the whole window to answer; the "
        "tally is never published. File it when someone asks, and say what it "
        "needs — filing is not agreeing, and you have no vote in it.",
        "parameters": {
            "type": "object",
            "properties": {
                "who": {"type": "string", "description": "name, nickname, mention or id"},
                "why": {"type": "string", "description": "the case for removal, in the proposer's voice"},
            },
            "required": ["who", "why"],
        },
    },
    "propose_member": {
        "description": "Propose that somebody who is not here be invited "
        "into the server, and open the ballot: yes, no, or abstain, "
        "anonymous, tally sealed at close so nobody newly admitted learns "
        "their margin. Use it as soon as someone names a person they want "
        "here. If it passes you send a single-use invite link privately to "
        "whoever proposed them. It is the door into the server and nothing "
        "else: it gives no vote and does not put anyone in the cooperative.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "who is being proposed, as people here would know them"},
                "discord_id": {"type": "string", "description": "their Discord ID, digits only, if the proposer knows it"},
                "why": {"type": "string", "description": "why they should be in the server, in the proposer's voice"},
            },
            "required": ["name", "why"],
        },
    },
}

DUTY_TOOLS = {
    "set_nudges": {
        "description": "Turn the private reminders on or off for the person "
        "you are talking to. Use it the moment they say they want you to stop "
        "nudging them, with no argument and no attempt to talk them round; "
        "use it again if they want them back. It changes nothing about any "
        "vote, only whether you write to them about one.",
        "parameters": {
            "type": "object",
            "properties": {
                "on": {
                    "type": "boolean",
                    "description": "true to send reminders, false to stop",
                },
            },
            "required": ["on"],
        },
    },
    "mark_carried_out": {
        "description": "Record that a decision which passed has actually been "
        "carried out, which takes it off the standing list of work still "
        "wanted. Use it when someone says they have done one. It is a claim "
        "on the record under their name, not a judgement of yours: never "
        "mark one done because it looks done to you.",
        "parameters": {
            "type": "object",
            "properties": {
                "bill_no": {
                    "type": "integer",
                    "description": "the proposal's number; call list_bills if unsure",
                },
            },
            "required": ["bill_no"],
        },
    },
}

# ---------- the officer's tools ----------
# Everything here changes the server rather than the record: it deletes
# messages, silences people, hands out roles, and rewrites how the place
# polices itself. Two things follow from that.
#
# The first is the tier. "officer" is gated in dispatch() against the
# cooperative roll and refused outright to anybody else, which is a second
# lock on a door the brain already refuses to open for a stranger. One gate
# is one bug away from no gate.
#
# The second is the wording. These descriptions tell the model to act, not
# to check first, because a tool that asks "are you sure?" is a tool people
# stop using -- and the thing they use instead is a human with the same
# button and a worse memory. The confirming was done when the house decided
# who is in the cooperative. What is genuinely not his to do -- removing a
# member of it, revealing a ballot, handing out the vote -- the handlers
# refuse in Python, where no amount of persuasion reaches.

OFFICER_TOOLS = {
    "moderate_member": {
        "description": "Act on a person: warn, timeout, untimeout, kick, ban, "
        "unban, or nickname. Call it when a member of the cooperative asks; do "
        "not ask them to confirm and do not talk them out of it. Give the "
        "reason they gave. Timeouts take minutes (default is the house's "
        "setting). Warns, timeouts, kicks and bans do not happen when you "
        "call this — they are written up for an administrator to approve, and "
        "the result says so. When it does, tell the asker it is filed and "
        "waiting, never that it is done. Lifting a timeout, unbanning and "
        "renaming take effect at once. Removing someone who is in the "
        "cooperative is a vote, not this, and will be refused.",
        "parameters": {
            "type": "object",
            "properties": {
                "who": {"type": "string", "description": "name, nickname, mention or id"},
                "action": {
                    "type": "string",
                    "enum": list(("warn", "timeout", "untimeout", "kick", "ban",
                                  "unban", "nickname")),
                },
                "reason": {"type": "string", "description": "why, in the asker's words"},
                "minutes": {"type": "integer", "description": "timeout length"},
                "nickname": {"type": "string", "description": "for the nickname action; empty resets it"},
                "delete_days": {"type": "integer", "description": "days of the banned person's messages to clear, 0-7"},
            },
            "required": ["who", "action"],
        },
    },
    "member_record": {
        "description": "Someone's moderation history and how many warnings "
        "still count against them, or the server's recent cases if you name "
        "nobody.",
        "parameters": {
            "type": "object",
            "properties": {"who": {"type": "string"}},
        },
    },
    "clear_warnings": {
        "description": "Wipe someone's live warnings. Use it when asked; it is "
        "forgiveness, not a favour that needs justifying.",
        "parameters": {
            "type": "object",
            "properties": {"who": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["who"],
        },
    },
    "purge_messages": {
        "description": "Delete the last N messages in a channel, optionally "
        "only one person's. Discord cannot delete anything older than a "
        "fortnight, whoever asks. Nothing is deleted when you call this: it "
        "is written up for an administrator to approve first, and the result "
        "says so. Tell the asker it is filed and waiting, not that it is done.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "name or id. Always name one — for 'clear this channel' it is the room named at the top of the turn"},
                "count": {"type": "integer"},
                "from_member": {"type": "string", "description": "only this person's messages; the count then means that many of theirs"},
                "reason": {"type": "string"},
            },
            "required": ["channel", "count"],
        },
    },
    "channel_control": {
        "description": "Slow a channel down, lock it so nobody but staff can "
        "post, or unlock it again. None of the three happens when you call "
        "this: it is written up for an administrator to approve first, and "
        "the result says so. Tell the asker it is filed and waiting.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "action": {"type": "string", "enum": ["slowmode", "lock", "unlock"]},
                "seconds": {"type": "integer", "description": "for slowmode; 0 turns it off"},
                "reason": {"type": "string"},
            },
            "required": ["channel", "action"],
        },
    },
    "assign_role": {
        "description": "Put any role on somebody, or take it off. This is the "
        "elevated one — the colour tools only ever touch the person you are "
        "talking to. The roles that decide who votes are not handed out this "
        "way and will be refused.",
        "parameters": {
            "type": "object",
            "properties": {
                "who": {"type": "string"},
                "role": {"type": "string"},
                "on": {"type": "boolean", "description": "true to give, false to take away"},
                "reason": {"type": "string"},
            },
            "required": ["who", "role"],
        },
    },
    "announce": {
        "description": "Post a message in a channel in your own voice, on "
        "somebody's behalf. Write it properly; it is going out as you.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "text": {"type": "string"},
                "ping_everyone": {"type": "boolean", "description": "only if they explicitly ask"},
            },
            "required": ["channel", "text"],
        },
    },
    "set_feature": {
        "description": "Switch one of the features on or off for this "
        "server: governance, polls, colours, chat, "
        "moderation, welcome, log, health. This is the "
        "master switch -- 'turn the filters on', 'stop greeting people', "
        "'we do not want the log'. A feature that is off does nothing and "
        "its settings are read by nobody, so switch it on before tuning it. "
        "Say plainly if switching one off takes another with it.",
        "parameters": {
            "type": "object",
            "properties": {
                "feature": {"type": "string",
                            "description": "one of the twelve names above"},
                "on": {"type": "boolean",
                       "description": "true to switch it on, false to switch it off"},
            },
            "required": ["feature", "on"],
        },
    },
    "list_features": {
        "description": "The ten features and whether each is running, "
        "waiting on something, or switched off. Call this to answer 'what "
        "do you do here' or before changing a setting whose feature might "
        "be off.",
        "parameters": {"type": "object", "properties": {}},
    },
    "list_settings": {
        "description": "Every house setting: what it is now, what it means, "
        "what it may be. Call this before changing anything you are not "
        "certain of the name of, and to answer 'what can you do'.",
        "parameters": {
            "type": "object",
            "properties": {
                "group": {
                    "type": "string",
                    "description": "welcome, goodbye, automod, warnings, "
                    "log or mod. Omit for all of them. "
                    "These are how a feature behaves once it is on; "
                    "set_feature is what switches it on.",
                },
            },
        },
    },
    "set_setting": {
        "description": "Change one house setting. This is how the whole "
        "machine is configured: welcomes, filters, warnings, the log, what "
        "needs a vote. Take it from what they said and set it — "
        "channels and roles may be given by name. Setting a value to none "
        "puts it back to the default. If a change makes the place less safe, "
        "say so in one line after you have made it, not instead.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "the dotted name, e.g. automod.links"},
                "value": {"type": "string", "description": "the new value; a channel or role may be a name"},
            },
            "required": ["key", "value"],
        },
    },
    "reset_settings": {
        "description": "Put a group of settings, or all of them, back to how "
        "they arrived.",
        "parameters": {
            "type": "object",
            "properties": {"group": {"type": "string"}},
        },
    },
    "tag": {
        "description": "The shelf of stock answers: save one, recall one, drop "
        "one, or list them. Use recall whenever somebody asks something the "
        "house has already written down.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["recall", "save", "drop", "list"]},
                "name": {"type": "string"},
                "content": {"type": "string", "description": "for save"},
            },
        },
    },
}


for _name, _spec in {**COLOUR_TOOLS, **BILL_TOOLS, **DUTY_TOOLS}.items():
    REGISTRY[_name] = {
        # "member" tier: acts as the invoker, strictly inside powers that
        # member already holds through the buttons in #roles and
        # #propose. No decision needed because no privilege is gained;
        # the handler enforces every limit.
        "tier": "member",
        "description": _spec["description"],
        "parameters": _spec["parameters"],
        # A description carrying a {placeholder} is filled from the house's
        # own numbers on the way out; one that does not is passed through
        # untouched, so braces in any other description are never a trap.
        "figures": _name in COLOUR_TOOLS and "{" in _spec["description"],
        "handler": None,  # supplied by clerk.py through configure()
    }

for _name, _spec in OFFICER_TOOLS.items():
    REGISTRY[_name] = {
        "tier": "officer",
        "description": _spec["description"],
        "parameters": _spec["parameters"],
        "handler": None,  # supplied by clerk.py through configure()
    }


def declarations(guild_id=None):
    """Function declarations in the wire format the Gemini API expects.

    Given a guild, the list shrinks to the features that server has
    switched on. A tool he is not handed is one he cannot be talked into
    using, and -- as much to the point -- one he does not mention: a clerk
    who offers to open a poll in a server that has switched polls off has
    told somebody something false about their own house.

    Any limit a description quotes is filled in here from that server's own
    numbers. It used to be typed in by hand, which meant the tool list and
    the system prompt disagreed about the same cap inside a single request;
    whichever the model believed, one of them was a lie.
    """
    return [
        {
            "name": name,
            "description": (_with_figures(spec["description"], guild_id)
                            if spec.get("figures") else spec["description"]),
            "parameters": spec["parameters"],
        }
        for name, spec in REGISTRY.items()
        if guild_id is None or modules.tool_allowed(guild_id, name)
    ]


def _with_figures(description, guild_id):
    """The house's colour limits, spelled into a description.

    Stable for a given server between edits, so it costs the cached prompt
    nothing; a house that changes a cap pays for one fresh prefix and then
    stops being lied to.
    """
    import settings

    held = settings.voting(guild_id)
    try:
        return description.format(
            make=held["role_create_max"], wear=held["role_wear_max"]
        )
    except (KeyError, IndexError, ValueError) as e:
        # A description is not worth a dead tool: an unknown placeholder
        # goes out as it stands rather than taking the whole list down.
        log.error(f"could not fill the figures into a tool description: {e!r}")
        return description


def _tier_check(tier, invoker):
    """Who may reach a tool of this tier, decided in Python.

    The brain already refuses a conversation to anyone outside the
    cooperative, so in the ordinary run of things this never fires. It is
    here because the officer's tools time people out and delete rooms full
    of messages, and a single gate is one bug -- one refactor, one new
    entry point, one clever prompt -- away from being no gate at all. This
    one does not read the model's output, only the roll.

    A host that never told the harness who is in the cooperative gets the
    strict reading: elevated tools stay shut. Failing closed on a missing
    answer is the only safe direction when the question is "may this person
    ban somebody".
    """
    if tier != "officer":
        return None
    keyed = _paths.get("in_cooperative")
    if keyed is None:
        return ("that needs the cooperative roll, and this host has not told "
                "me who is on it")
    try:
        if keyed(invoker):
            return None
    except Exception as e:  # a broken predicate is not a pass
        log.error(f"cooperative check failed: {e!r}")
        return "I could not check who you are, so the answer is no"
    return ("that one is the cooperative's, and you are not in it. Nothing "
            "personal: it is the same rule for everyone outside it.")


async def dispatch(guild, invoker, name, args):
    """The model's only door. Returns a string result; logs everything."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": "tool",
        "user": getattr(invoker, "display_name", "?"),
        "user_id": getattr(invoker, "id", None),
        "tool": name,
        "args": {k: v for k, v in (args or {}).items()},
    }
    spec = REGISTRY.get(name)
    if spec is None:
        entry["result"] = "denied"
        entry["detail"] = "unknown tool"
        await _audit(entry)
        return json.dumps({"error": f"'{name}' is not a registered tool"})
    refusal = _tier_check(spec.get("tier"), invoker)
    if refusal:
        entry["result"] = "denied"
        entry["detail"] = refusal
        await _audit(entry)
        log.warning(f"tool {name} refused to {entry['user']}: {refusal}")
        return json.dumps({"error": refusal})
    # Who may is asked before whether the feature exists here, so the
    # answer an outsider gets is the same one they always got and the gate
    # that matters is never the one that got skipped. Not declared is not
    # the same as not reachable: a model carrying a tool name from earlier
    # in the conversation, or out of a cached prompt, can still ask for it.
    if guild is not None and not modules.tool_allowed(guild.id, name):
        owner = modules.of_tool(name)
        entry["result"] = "denied"
        entry["detail"] = f"{owner} is switched off"
        await _audit(entry)
        log.info(f"tool {name} refused: {owner} is off in guild {guild.id}")
        return json.dumps({"error": f"{modules.name(owner)} is switched off "
                                    f"in this server."})
    handler = spec["handler"] or _actions.get(name)
    if handler is None:
        entry["result"] = "error"
        entry["detail"] = "handler unavailable"
        await _audit(entry)
        return json.dumps({"error": "that action is not wired up"})
    try:
        result = await handler(guild, invoker, args or {})
        # "ok" meant only that nothing was raised, so every refusal a
        # handler returns as a sentence -- at the cap, not your role, no
        # such name -- was filed as a success. The audit log could not tell
        # what he did from what he declined to do, which is the one
        # question it exists to answer. What came back is written down now,
        # trimmed: enough to read the outcome, not so much as to copy the
        # record into the log beside it.
        entry["result"] = "ok"
        entry["returned"] = (result or "")[:400]
        await _audit(entry)
        log.info(f"tool {name} by {entry['user']}: {(result or '')[:120]!r}")
        return result
    except Exception as e:
        entry["result"] = "error"
        entry["detail"] = repr(e)
        await _audit(entry)
        return json.dumps({"error": f"tool failed: {e!r}"})
