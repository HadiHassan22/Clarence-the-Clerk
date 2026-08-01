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


async def _lookup(guild, invoker, args):
    """Whichever drawer of the record was asked for.

    The five handlers underneath are unchanged and still take their own
    arguments; this only works out which one was meant. A `kind` that is
    not one of the five comes back naming them, because a refusal that
    says what would have worked is one the model can act on.
    """
    kind = str(args.get("kind") or "").strip().lower()
    number = args.get("number")
    if kind == "rules":
        return await _get_standing_orders(guild, invoker, {})
    if kind == "bills":
        return await _list_bills(guild, invoker, args)
    if kind == "acts":
        return await _list_acts(guild, invoker, args)
    if kind == "bill":
        if number is None:
            return json.dumps({"error": "which proposal? give me its number"})
        return await _get_bill(guild, invoker, {"bill_no": number})
    if kind == "act":
        if number is None:
            return json.dumps({"error": "which decision? give me its number"})
        return await _get_act(guild, invoker, {"act_no": number})
    return json.dumps(
        {"error": f"{kind!r} is not something to look up; kind is one of "
                  f"bills, bill, acts, act, rules"}
    )


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
                if not m.bot and keyed(m)
                and roster.is_away(guild.id, m, away_days)
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
    # One door onto the record instead of five.
    #
    # get_standing_orders, list_acts, get_act, list_bills and get_bill were
    # five tools over one filing cabinet, which is five chances to reach for
    # the wrong one -- and the wrong one usually answers, plausibly, about
    # the wrong thing. An enum is far harder to get wrong than a tool name,
    # and the model picks the drawer rather than the door.
    "lookup": {
        "tier": "minor",
        "description": "Read the record. `kind` picks what: 'bills' for the "
        "index of proposals (add `status` to narrow it; 'on_floor' means "
        "still open), 'bill' for one in full with `number`, 'acts' for the "
        "index of decisions, 'act' for one in full with `number`, 'rules' "
        "for the full standing orders. Call this before answering anything "
        "about what is open, what was decided, or what the rules say. "
        "Individual ballots are sealed and never come back from any of it.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["bills", "bill", "acts", "act", "rules"],
                },
                "number": {
                    "type": "integer",
                    "description": "the proposal or decision number, for 'bill' and 'act'",
                },
                "status": {
                    "type": "string",
                    "enum": ["on_floor", "passed", "failed", "all"],
                    "description": "for 'bills' only",
                },
                "limit": {"type": "integer", "description": "for 'acts' only, up to 50"},
            },
            "required": ["kind"],
        },
        "handler": _lookup,
    },

    "server_info": {
        "tier": "minor",
        "description": "The server's structure: categories, channels, topics, member count.",
        "parameters": {"type": "object", "properties": {}},
        "handler": _server_info,
    },
}


BILL_TOOLS = {
    # One door onto filing, for the same reason `lookup` is one onto
    # reading: three tools that all put something to a vote is three
    # chances to pick the wrong one, and picking `propose_removal` when
    # somebody meant `propose_member` is not a recoverable mistake.
    "propose": {
        "description": "Put something to the cooperative, in the name of the "
        "person you are talking to. Use it the moment they say they want "
        "something: draft it from what they said, file it, tell them the "
        "number. Do not ask them to confirm and do not send them to a "
        "button. Filing decides nothing and is not agreeing -- the "
        "cooperative votes and you have no say in it.\n"
        "`kind` picks which:\n"
        "- 'change' -- anything the house should decide. Needs `title`, "
        "`what` (the operative text, what becomes true if it passes) and "
        "`why` (their reasons, in their voice).\n"
        "- 'invite' -- somebody not in the server, proposed into it. Needs "
        "`who` and `why`. What passes is a single-use link sent privately to "
        "whoever proposed them; it gives no vote and puts nobody in the "
        "cooperative.\n"
        "- 'removal' -- a member of the cooperative, proposed out of it. "
        "Needs `who` and `why`. This is the only route: you cannot remove "
        "one of them yourself and nor can anybody else, because it is a "
        "fundamental vote. The subject cannot vote, keeps the whole window "
        "to answer, and the tally is never published.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["change", "invite", "removal"],
                },
                "title": {"type": "string", "description": "for 'change': a short name for it"},
                "what": {"type": "string", "description": "for 'change': what becomes true if it passes"},
                "why": {"type": "string", "description": "their reasons, in their voice. Required for all three"},
                "who": {"type": "string", "description": "for 'invite' and 'removal': the person, as people here would know them"},
                "discord_id": {"type": "string", "description": "for 'invite': their Discord ID, digits only, if the proposer knows it"},
                "priority": {"type": "boolean", "description": "for 'change': true only if they ask you to chase people about it. It direct-messages everyone who has not voted, so it is for the ones that matter, not the ones they are keen on"},
            },
            "required": ["kind", "why"],
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
    "set_feature": {
        "description": "Switch one of the features on or off for this "
        "server: governance, chat, "
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
        "description": "The numbers this house votes by: how long a vote "
        "stays open, what share carries a removal or a rule change, the "
        "quorum on a public poll, and how long a quiet spell takes somebody "
        "out of the count. Says what each one is now, what it means, what it "
        "defaults to, and the range it has to stay inside. Read this before "
        "changing one rather than guessing at a name.",
        "parameters": {"type": "object", "properties": {}},
    },
    "set_setting": {
        "description": "Change one of the numbers this house votes by. Held "
        "inside its bounds, so a value that would break the machinery comes "
        "back refused with the range it had to be in. These are the "
        "cooperative's own rules: change one when somebody asks, say what it "
        "was and what it is, and never argue about which way it should go.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "the exact name, from list_settings"},
                "value": {"type": "number"},
            },
            "required": ["key", "value"],
        },
    },
    "reset_settings": {
        "description": "Put every number back to the one he arrived with, and "
        "say which of them this house had changed.",
        "parameters": {"type": "object", "properties": {}},
    },
}


for _name, _spec in {**BILL_TOOLS, **DUTY_TOOLS}.items():
    REGISTRY[_name] = {
        # "member" tier: acts as the invoker, strictly inside powers that
        # member already holds through the buttons in #roles and
        # #propose. No decision needed because no privilege is gained;
        # the handler enforces every limit.
        "tier": "member",
        "description": _spec["description"],
        "parameters": _spec["parameters"],
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
    who offers a feature the server has switched off has told somebody
    something false about their own house.

    """
    return [
        {
            "name": name,
            "description": spec["description"],
            "parameters": spec["parameters"],
        }
        for name, spec in REGISTRY.items()
        if guild_id is None or modules.tool_allowed(guild_id, name)
    ]


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
