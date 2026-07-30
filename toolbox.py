"""The harness: the only door between Clarence's brain and reality.

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
from datetime import datetime, timezone
from pathlib import Path

# configured by clerk.py at startup
_paths = {}
_log_lock = asyncio.Lock()

PEOPLE_KINDS = ("invite", "kick")


def configure(here: Path, data: Path):
    _paths["here"] = here
    _paths["data"] = data
    _paths["log"] = data / "executor_log.json"


def _load(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


async def _audit(entry):
    async with _log_lock:
        log = _load(_paths["log"], [])
        log.append(entry)
        _paths["log"].write_text(json.dumps(log, indent=2))


# ---------- handlers (all read-only in stage 2) ----------

async def _get_charter(guild, invoker, args):
    return (_paths["here"] / "constitution.md").read_text()


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
                a.pop("tally", None)  # sealed by order of the house
            return json.dumps(a)
    return json.dumps({"error": f"no Act {act_no} on record"})


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
    return json.dumps({"error": f"no Bill No. {bill_no} on record"})


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
    return json.dumps(
        {
            "server": guild.name,
            "members": guild.member_count,
            "top_level": top,
            "categories": cats,
        }
    )


# ---------- registry ----------

REGISTRY = {
    "get_charter": {
        "tier": "minor",
        "description": "The founding charter of the server, full text.",
        "parameters": {"type": "object", "properties": {}},
        "handler": _get_charter,
    },
    "get_standing_orders": {
        "tier": "minor",
        "description": "The standing orders (rules of procedure), full text.",
        "parameters": {"type": "object", "properties": {}},
        "handler": _get_standing_orders,
    },
    "list_acts": {
        "tier": "minor",
        "description": "Index of passed Acts: number, title, author, date.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "max entries, up to 50"}},
        },
        "handler": _list_acts,
    },
    "get_act": {
        "tier": "minor",
        "description": "Full text and details of one passed Act by number.",
        "parameters": {
            "type": "object",
            "properties": {"act_no": {"type": "integer"}},
            "required": ["act_no"],
        },
        "handler": _get_act,
    },
    "list_bills": {
        "tier": "minor",
        "description": "Index of bills: number, title, kind, status, author.",
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
        "description": "Full details of one bill: what, why, status, notes. "
        "Individual ballots are sealed and never available.",
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
    try:
        result = await spec["handler"](guild, invoker, args or {})
        entry["result"] = "ok"
        await _audit(entry)
        return result
    except Exception as e:
        entry["result"] = "error"
        entry["detail"] = repr(e)
        await _audit(entry)
        return json.dumps({"error": f"tool failed: {e!r}"})
