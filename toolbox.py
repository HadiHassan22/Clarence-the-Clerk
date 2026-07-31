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

log = logging.getLogger("toolbox")

# configured by clerk.py at startup
_paths = {}
_actions = {}
_log_lock = asyncio.Lock()

PEOPLE_KINDS = ("invite", "kick")


def configure(here: Path, data: Path, actions=None, in_cooperative=None):
    """actions: hand-written handlers injected by clerk.py, so the harness
    never reaches into the bot's internals itself.

    in_cooperative decides who a vote is counted against. Without it the
    roster is simply left out of what `server_info` reports, rather than
    guessed at -- a wrong threshold is worse than a missing one."""
    _paths["here"] = here
    _paths["data"] = data
    _paths["log"] = data / "executor_log.json"
    _paths["in_cooperative"] = in_cooperative
    _actions.update(actions or {})


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


# ---------- the memory book ----------

MEM_CAP = 150
MEM_KINDS = ("fact", "joke", "preference", "lore")


def _mem_path():
    return _paths["data"] / "clerk_memory.json"


def load_memories():
    return _load(_mem_path(), [])


def save_memories(entries):
    _atomic(_mem_path(), entries)


def add_memory(kind, about, text, source="conversation"):
    """File a memory. Deduped, capped, oldest low-value entries fall off."""
    kind = kind if kind in MEM_KINDS else "fact"
    text = (text or "").strip()[:240]
    about = (about or "the server").strip()[:60]
    if not text:
        return "nothing to file"
    entries = load_memories()
    for e in entries:
        if e["about"].lower() == about.lower() and e["text"].lower() == text.lower():
            return "already on record"
    entries.append(
        {
            "id": max((e["id"] for e in entries), default=0) + 1,
            "kind": kind,
            "about": about,
            "text": text,
            "learned_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "source": source,
        }
    )
    if len(entries) > MEM_CAP:
        entries = entries[-MEM_CAP:]
    save_memories(entries)
    log.info(f"memory filed [{kind}] {about}: {text}")
    return f"filed: [{kind}] {about}: {text}"


async def _remember(guild, invoker, args):
    return add_memory(
        args.get("kind", "fact"),
        args.get("about"),
        args.get("text", ""),
        source=f"filed during conversation with {invoker.display_name}",
    )


async def _forget(guild, invoker, args):
    """Strike memories. Python-enforced: only the subject of a memory may
    have it struck (house-wide memories are strikeable by anyone)."""
    query = (args.get("query") or "").strip().lower()
    if not query:
        return json.dumps({"error": "a query is required"})
    entries = load_memories()
    allowed_about = {invoker.display_name.lower(), "the server", "the house"}
    struck, kept = [], []
    for e in entries:
        matches = query in e["text"].lower() or query in e["about"].lower()
        if matches and e["about"].lower() in allowed_about:
            struck.append(e)
        else:
            kept.append(e)
    if not struck:
        return (
            "Nothing struck. Either no memory matches, or the memory is "
            "about someone else: only its subject may have it removed."
        )
    save_memories(kept)
    return f"struck {len(struck)} memor{'y' if len(struck) == 1 else 'ies'} from the book"


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

        size = len(roster.active(guild, keyed))
        info["roster"] = {
            "counted": size,
            "away": sum(
                1 for m in guild.members
                if not m.bot and keyed(m) and roster.is_away(m)
            ),
            "normal_needs": roster.required(size, "normal"),
            "fundamental_needs": roster.required(size, "fundamental"),
            "note": "counted now, from who is here; thresholds are shares "
                    "of this, never fixed numbers",
        }
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
    "remember": {
        "tier": "minor",
        "description": "File a memory in your book: a fact about a member, a "
        "running joke, a preference, or house lore. Use for things still "
        "worth knowing in a month; never for votes or private matters.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(MEM_KINDS)},
                "about": {
                    "type": "string",
                    "description": "who or what it concerns; a display name, or 'the server'",
                },
                "text": {"type": "string", "description": "the memory, one sentence"},
            },
            "required": ["kind", "about", "text"],
        },
        "handler": _remember,
    },
    "forget": {
        "tier": "minor",
        "description": "Strike memories matching a query from your book. Only "
        "works on memories about the person asking (or about the server).",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "handler": _forget,
    },
}


COLOUR_TOOLS = {
    "list_color_roles": {
        "description": "Every custom colour role: name, hex colour, how many "
        "wear it, who made it, and whether the person asking made it.",
        "parameters": {"type": "object", "properties": {}},
    },
    "create_color_role": {
        "description": "Create a colour role for the person you are talking "
        "to. They may have five at most. Purely cosmetic: a name and a colour.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "color": {"type": "string", "description": "hex, e.g. #ff9d2e"},
            },
            "required": ["name", "color"],
        },
    },
    "edit_color_role": {
        "description": "Rename or recolour a role the person you are talking "
        "to created. Only its creator may change it.",
        "parameters": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "the role's current name"},
                "name": {"type": "string"},
                "color": {"type": "string"},
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
        "description": "Put a colour role on the person you are talking to. "
        "Anyone may wear anyone's colour; five at a time.",
        "parameters": {
            "type": "object",
            "properties": {"role": {"type": "string"}},
            "required": ["role"],
        },
    },
    "shed_color_role": {
        "description": "Take a colour role off the person you are talking to.",
        "parameters": {
            "type": "object",
            "properties": {"role": {"type": "string"}},
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
    "propose_member": {
        "description": "Propose that someone be let in, and open the ballot: "
        "yes, no, or abstain, anonymous, tally sealed at close so nobody "
        "newly admitted learns their margin. Use it as soon as someone names "
        "a person they want in. If it passes you send a single-use invite "
        "link privately to whoever proposed them.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "who is being proposed, as people here would know them"},
                "discord_id": {"type": "string", "description": "their Discord ID, digits only, if the proposer knows it"},
                "why": {"type": "string", "description": "why we should let them in, in the proposer's voice"},
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

for _name, _spec in {**COLOUR_TOOLS, **BILL_TOOLS, **DUTY_TOOLS}.items():
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


def declarations():
    """Function declarations in the wire format the Gemini API expects."""
    return [
        {
            "name": name,
            "description": spec["description"],
            "parameters": spec["parameters"],
        }
        for name, spec in REGISTRY.items()
    ]


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
    handler = spec["handler"] or _actions.get(name)
    if handler is None:
        entry["result"] = "error"
        entry["detail"] = "handler unavailable"
        await _audit(entry)
        return json.dumps({"error": "that action is not wired up"})
    try:
        result = await handler(guild, invoker, args or {})
        entry["result"] = "ok"
        await _audit(entry)
        log.info(f"tool {name} by {entry['user']}: ok")
        return result
    except Exception as e:
        entry["result"] = "error"
        entry["detail"] = repr(e)
        await _audit(entry)
        return json.dumps({"error": f"tool failed: {e!r}"})
