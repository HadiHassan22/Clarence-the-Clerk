"""The house rules Eugene keeps without a vote: settings, and the engines
that read them.

Everything a moderation bot does -- welcomes, automod, warning
escalation, a log, a shelf of saved answers -- is a policy plus a switch.
This module is the switches and the policy; nothing here imports discord
and nothing here talks to anybody. That is deliberate: the whole point of
letting a conversation change a setting is that the setting can be wrong,
and a thing that can be wrong should be testable without a server.

There are no admin panels and no config file to edit. A member of the
cooperative says "stop deleting links, we post GitHub all day" and the
model writes the key. So every key is declared here with a type, bounds
and a sentence of help: the declaration is simultaneously the validator,
the documentation the model reads, and the list it is allowed to touch.
A key that is not in SPEC cannot be set by anyone, however nicely they ask.

Ids, never names. A channel or a role is stored as an integer, because a
server that renames #general at midnight should not wake up with its
welcomes going nowhere. Turning "the general channel" into that integer
is the job of the Discord half, in powers.py; this half only ever sees
the number.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import settings

log = logging.getLogger("warden")

# One settings key holds the whole table, so a server's choices read as one
# object and a key nobody has touched keeps following the default rather
# than being frozen at whatever it was the day they changed something else.
KEY = "warden"

TIMEOUT_MAX_MINUTES = 40320  # Discord's own ceiling: 28 days.


# ---------- what may be set ----------
#
# type: bool | int | text | choice | list | channel | role | map
#   channel/role are integers with a name attached in conversation; the
#   Discord half resolves the name and stores the id.
#   map is level -> role id, and is the only nested shape here.
#
# Every entry carries its own help line because that line is what the model
# is shown when someone asks what can be changed. A key whose help does not
# explain itself will be set wrong.

# There is no `<group>.enabled` in here. Whether a feature runs at all is a
# question `modules.py` answers, and it was answered twice for a while: a
# server could tick Levels on the setup panel, watch it go green, and get
# nothing, because `automod.enabled` was still false underneath. Two switches
# for one question is one switch and one bug. What is left in here is how a
# feature behaves once it is on, which is the only thing a settings table
# should have an opinion about.
#
# `goodbye.enabled` is the exception and stays, because it is not a master
# switch: greeting arrivals without announcing departures is a thing servers
# actually want, and both live in the same module.
#
# There is no `welcome.channel` or `goodbye.channel` in here either, for the
# same reason. Where a room is, is a binding -- one table, by id, set in one
# place -- and having a second answer in the settings meant two ways to say
# where the greeting goes, which is one way too many and the sort of thing
# that ends with a server being greeted twice.

SPEC = {
    # ----- arrivals -----
    "welcome.message": {
        "type": "text", "default":
            "Welcome {mention} to {server}. You are member {count}.",
        "help": "The greeting. Placeholders: {mention} {user} {server} {count}.",
    },
    "welcome.dm": {
        "type": "text", "default": "",
        "help": "A private note to each arrival. Empty sends none.",
    },
    "welcome.role": {
        "type": "role", "default": None,
        "help": "A role put on everyone who arrives.",
    },
    "goodbye.enabled": {
        "type": "bool", "default": False,
        "help": "Say something when someone leaves, in the same room as the "
                "greeting. For departures in a private room instead, that is "
                "`log.joins`.",
    },
    "goodbye.message": {
        "type": "text", "default": "{user} has left. {count} of us now.",
        "help": "Placeholders: {user} {server} {count}.",
    },

    # ----- automod -----
    "automod.exempt_cooperative": {
        "type": "bool", "default": True,
        "help": "Whether the cooperative is above the filters.",
    },
    "automod.exempt_channels": {
        "type": "list", "default": [],
        "help": "Channel ids where nothing is filtered.",
    },
    "automod.banned_words": {
        "type": "list", "default": [],
        "help": "Words that trip the filter. Matched whole, case-blind.",
    },
    "automod.banned_words_action": {
        "type": "choice", "choices": ["off", "delete", "warn", "timeout"],
        "default": "delete", "help": "What a banned word costs.",
    },
    "automod.invites": {
        "type": "choice", "choices": ["off", "delete", "warn", "timeout"],
        "default": "off", "help": "What an invite link to another server costs.",
    },
    "automod.links": {
        "type": "choice", "choices": ["off", "delete", "warn", "timeout"],
        "default": "off",
        "help": "What a link costs, unless its host is on the allowlist.",
    },
    "automod.link_allowlist": {
        "type": "list",
        "default": ["youtube.com", "youtu.be", "github.com", "tenor.com",
                    "discord.com"],
        "help": "Hosts the link rule ignores. Subdomains count.",
    },
    "automod.mass_mentions": {
        "type": "int", "default": 0, "min": 0, "max": 50,
        "help": "Mentions in one message before it trips. 0 is off.",
    },
    "automod.mass_mentions_action": {
        "type": "choice", "choices": ["off", "delete", "warn", "timeout"],
        "default": "warn", "help": "What a mention pile-up costs.",
    },
    "automod.spam_messages": {
        "type": "int", "default": 0, "min": 0, "max": 30,
        "help": "Messages inside the spam window before it trips. 0 is off.",
    },
    "automod.spam_seconds": {
        "type": "int", "default": 10, "min": 2, "max": 300,
        "help": "How long that window is.",
    },
    "automod.spam_action": {
        "type": "choice", "choices": ["off", "delete", "warn", "timeout"],
        "default": "timeout", "help": "What flooding costs.",
    },
    "automod.caps_percent": {
        "type": "int", "default": 0, "min": 0, "max": 100,
        "help": "Share of letters in capitals before it trips. 0 is off.",
    },
    "automod.caps_min_length": {
        "type": "int", "default": 12, "min": 4, "max": 200,
        "help": "Messages shorter than this are never shouting.",
    },
    "automod.caps_action": {
        "type": "choice", "choices": ["off", "delete", "warn", "timeout"],
        "default": "delete", "help": "What shouting costs.",
    },
    "automod.timeout_minutes": {
        "type": "int", "default": 10, "min": 1, "max": TIMEOUT_MAX_MINUTES,
        "help": "How long an automod timeout lasts.",
    },

    # ----- warnings -----
    "warnings.timeout_at": {
        "type": "int", "default": 3, "min": 0, "max": 20,
        "help": "Warnings that add up to a timeout. 0 never escalates.",
    },
    "warnings.timeout_minutes": {
        "type": "int", "default": 60, "min": 1, "max": TIMEOUT_MAX_MINUTES,
        "help": "How long that timeout lasts.",
    },
    "warnings.expire_days": {
        "type": "int", "default": 30, "min": 0, "max": 3650,
        "help": "Days a warning counts for. 0 means forever.",
    },

    # ----- the log -----
    "log.channel": {
        "type": "channel", "default": None,
        "help": "Where the record of what happened goes. Unset, it falls back "
                "to the health room rather than going quiet: a moderation "
                "action is public by the standing orders, and the wrong room "
                "is a smaller failure than no room.",
    },
    "log.deletes": {"type": "bool", "default": False, "help": "Log deleted messages."},
    "log.edits": {"type": "bool", "default": False, "help": "Log edited messages."},
    "log.joins": {"type": "bool", "default": False, "help": "Log arrivals and departures."},
    "log.mod": {"type": "bool", "default": True, "help": "Log every moderation action."},

    # ----- moderation policy -----
    "mod.protect_cooperative": {
        "type": "bool", "default": True,
        "help": "Whether removing a member of the cooperative needs a vote "
                "rather than a word. Turning this off lets one person remove "
                "another with no ballot.",
    },
    "mod.dm_on_action": {
        "type": "bool", "default": True,
        "help": "Tell people privately when something is done to them, and why.",
    },
    "mod.default_timeout_minutes": {
        "type": "int", "default": 10, "min": 1, "max": TIMEOUT_MAX_MINUTES,
        "help": "How long a timeout lasts when nobody says.",
    },
    "mod.purge_max": {
        "type": "int", "default": 100, "min": 1, "max": 500,
        "help": "The most messages one sweep may delete.",
    },
    "mod.require_signoff": {
        "type": "bool", "default": True,
        "help": "Whether a warning, timeout, kick, ban, sweep or channel "
                "change asked for in conversation waits for an administrator "
                "to approve it first. On, nothing happens until somebody "
                "presses Approve on the card in the log room. Off, Eugene "
                "acts on the word of anyone in the cooperative, as he used "
                "to. The filters are not affected either way: automod acts "
                "on the message in front of it.",
    },
    "mod.signoff_minutes": {
        "type": "int", "default": 60, "min": 5, "max": 10080,
        "help": "How long a request stands on the desk before it lapses "
                "unsigned. Lapsing does nothing — it is the safe end.",
    },
}

GROUPS = ("welcome", "goodbye", "automod", "warnings", "log", "mod")

TRUE_WORDS = {"true", "yes", "on", "1", "enable", "enabled"}
FALSE_WORDS = {"false", "no", "off", "0", "disable", "disabled"}


# ---------- reading and writing settings ----------

def _table(guild_id) -> dict:
    got = settings.get(guild_id, KEY) or {}
    return got if isinstance(got, dict) else {}


def get(guild_id, key):
    """One value: what the house chose, or what it came with."""
    spec = SPEC.get(key)
    if spec is None:
        return None
    stored = _table(guild_id)
    if key in stored:
        ok, value = coerce(key, stored[key])
        if ok:
            return value
    default = spec["default"]
    return dict(default) if isinstance(default, dict) else (
        list(default) if isinstance(default, list) else default)


def config(guild_id) -> dict:
    """Every key, complete. Never partial: half a policy is not a policy,
    and a caller reading `automod.links` must never get None because
    nobody has opened that menu."""
    return {key: get(guild_id, key) for key in SPEC}


def overrides(guild_id) -> dict:
    """Only what this house actually chose, for saying which settings are
    theirs and which are simply the ones he arrived with."""
    stored = _table(guild_id)
    return {k: v for k, v in stored.items() if k in SPEC}


def coerce(key, value):
    """(ok, value_or_reason). Everything arrives from a language model by
    way of a person talking, so "true", True and "on" all mean the same
    thing and none of them may be stored as a string where a switch goes."""
    spec = SPEC.get(key)
    if spec is None:
        return False, f"'{key}' is not a setting"
    kind = spec["type"]
    if value is None:
        return True, None
    try:
        if kind == "bool":
            if isinstance(value, bool):
                return True, value
            word = str(value).strip().lower()
            if word in TRUE_WORDS:
                return True, True
            if word in FALSE_WORDS:
                return True, False
            return False, "that wants a yes or a no"
        if kind == "int":
            number = int(str(value).strip())
            low, high = spec.get("min", 0), spec.get("max", 10 ** 9)
            return True, max(low, min(high, number))
        if kind in ("channel", "role"):
            number = int(str(value).strip())
            return (True, number) if number > 0 else (True, None)
        if kind == "text":
            return True, str(value)[:1500]
        if kind == "choice":
            word = str(value).strip().lower()
            if word not in spec["choices"]:
                return False, f"pick one of: {', '.join(spec['choices'])}"
            return True, word
        if kind == "list":
            if isinstance(value, str):
                parts = re.split(r"[,\n]", value)
            else:
                parts = list(value)
            cleaned, seen = [], set()
            for part in parts:
                item = str(part).strip().lower()
                if item and item not in seen:
                    seen.add(item)
                    cleaned.append(item[:80])
            return True, cleaned[:200]
        if kind == "map":
            if isinstance(value, str):
                value = json.loads(value)
            if not isinstance(value, dict):
                return False, "that wants a table of level to role"
            out = {}
            for k, v in list(value.items())[:50]:
                out[str(int(k))] = int(v)
            return True, out
    except (TypeError, ValueError, json.JSONDecodeError):
        return False, f"that is not a {kind}"
    return False, f"unknown setting type {kind}"


def set_value(guild_id, key, value):
    """(ok, stored_or_reason). Passing None puts a key back on its default
    rather than pinning it, so a house that changes its mind is not left
    holding a copy of a value it never chose."""
    if key not in SPEC:
        return False, f"'{key}' is not a setting. Ask for the list."
    ok, held = coerce(key, value)
    if not ok:
        return False, held
    table = dict(_table(guild_id))
    if held is None or (SPEC[key]["type"] == "list" and not held):
        table.pop(key, None)
        settings.put(guild_id, **{KEY: table or None})
        return True, SPEC[key]["default"]
    table[key] = held
    settings.put(guild_id, **{KEY: table})
    return True, held


def reset(guild_id, prefix=None):
    """Put a group, or the lot, back to how it arrived."""
    table = dict(_table(guild_id))
    dropped = [k for k in table if prefix is None or k.startswith(prefix)]
    for key in dropped:
        table.pop(key)
    settings.put(guild_id, **{KEY: table or None})
    return dropped


def describe(group=None):
    """The settings, as the model reads them: name, type, bounds, help."""
    out = {}
    for key, spec in SPEC.items():
        if group and not key.startswith(group):
            continue
        line = {"type": spec["type"], "help": spec["help"],
                "default": spec["default"]}
        if "choices" in spec:
            line["choices"] = spec["choices"]
        if "min" in spec:
            line["range"] = [spec["min"], spec["max"]]
        out[key] = line
    return out


# ---------- automod ----------

INVITE = re.compile(r"(?:discord\.(?:gg|me|io)|discord(?:app)?\.com/invite)/\w+",
                    re.I)
LINK = re.compile(r"https?://([^\s/]+)", re.I)
MENTION = re.compile(r"<@[!&]?\d+>|@everyone|@here")

# Ordered: a message that trips two rules pays the higher price once.
SEVERITY = {"off": 0, "delete": 1, "warn": 2, "timeout": 3}


def _word_hit(text, words):
    lowered = f" {re.sub(r'[^a-z0-9]+', ' ', text.lower())} "
    for word in words:
        needle = re.sub(r"[^a-z0-9]+", " ", word.lower()).strip()
        if needle and f" {needle} " in lowered:
            return word
    return None


def _foreign_links(text, allowlist):
    out = []
    for host in LINK.findall(text):
        host = host.lower().split(":")[0]
        if not any(host == ok or host.endswith("." + ok) for ok in allowlist):
            out.append(host)
    return out


def caps_share(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0
    return round(100 * sum(1 for c in letters if c.isupper()) / len(letters))


def scan(cfg, text, *, mention_count=0, recent=()):
    """Every rule this message trips, worst first.

    `recent` is the timestamps of that author's last messages here, newest
    last, and it is the caller's business to keep: this half counts, it
    does not remember. Returns [] when nothing is wrong, which is the
    common case and the one that must stay cheap.
    """
    hits = []

    def add(rule, action, detail):
        if SEVERITY.get(action, 0) > 0:
            hits.append({"rule": rule, "action": action, "detail": detail})

    word = _word_hit(text, cfg.get("automod.banned_words") or [])
    if word:
        add("banned word", cfg.get("automod.banned_words_action"), word)

    if cfg.get("automod.invites") != "off" and INVITE.search(text):
        add("invite link", cfg.get("automod.invites"), "an invite to another server")

    if cfg.get("automod.links") != "off":
        foreign = _foreign_links(text, cfg.get("automod.link_allowlist") or [])
        if foreign:
            add("link", cfg.get("automod.links"), foreign[0])

    ceiling = cfg.get("automod.mass_mentions") or 0
    if ceiling and mention_count >= ceiling:
        add("mass mentions", cfg.get("automod.mass_mentions_action"),
            f"{mention_count} mentions")

    floor = cfg.get("automod.caps_percent") or 0
    if floor and len(text) >= (cfg.get("automod.caps_min_length") or 12):
        share = caps_share(text)
        if share >= floor:
            add("shouting", cfg.get("automod.caps_action"), f"{share}% capitals")

    limit = cfg.get("automod.spam_messages") or 0
    if limit:
        window = cfg.get("automod.spam_seconds") or 10
        cutoff = time.time() - window
        burst = sum(1 for t in recent if t >= cutoff)
        if burst >= limit:
            add("flooding", cfg.get("automod.spam_action"),
                f"{burst} messages in {window}s")

    return sorted(hits, key=lambda h: -SEVERITY.get(h["action"], 0))


def verdict(hits):
    """What actually happens to a message that tripped `hits`: the worst
    single action, and every reason, so the person is told all of it and
    punished once."""
    if not hits:
        return None
    worst = hits[0]["action"]
    return {"action": worst,
            "rules": [h["rule"] for h in hits],
            "reason": "; ".join(f"{h['rule']} ({h['detail']})" for h in hits)}


# ---------- the stores ----------

def _atomic(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _read(guild_id, name, default):
    path = settings.state_file(guild_id, name)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # A corrupt side-store must not take the server down with it; a lost
        # setting is recoverable, a dead clerk is not.
        log.warning(f"{name} for guild {guild_id} is unreadable; starting fresh")
        return default


def _write(guild_id, name, data):
    _atomic(settings.state_file(guild_id, name), data)


def now():
    return datetime.now(timezone.utc)


# ----- the case book -----

CASES = "warden_cases.json"
CASE_CAP = 5000


def add_case(guild_id, kind, *, target_id, target_name, moderator, reason,
             detail=None):
    """Every act of moderation gets a number and a line. This is the part
    that makes it reviewable afterwards: who did what to whom, and why. It
    is written before the action is attempted, so a failure leaves a trace
    rather than nothing."""
    book = _read(guild_id, CASES, [])
    case = {
        "case": (book[-1]["case"] + 1) if book else 1,
        "kind": kind,
        "target_id": int(target_id) if target_id else None,
        "target": target_name,
        "moderator": moderator,
        "reason": (reason or "").strip()[:500] or "no reason given",
        "detail": detail,
        "at": now().isoformat(),
        "cleared": False,
    }
    book.append(case)
    _write(guild_id, CASES, book[-CASE_CAP:])
    return case


def cases(guild_id, target_id=None, kind=None, limit=25):
    book = _read(guild_id, CASES, [])
    out = [
        c for c in book
        if (target_id is None or c.get("target_id") == int(target_id))
        and (kind is None or c.get("kind") == kind)
    ]
    return out[-limit:]


def live_warnings(guild_id, target_id, expire_days=None):
    """Warnings that still count: not cleared, not expired. The expiry is a
    setting because a house that forgives is a different house from one
    that does not, and neither is our business to decide."""
    if expire_days is None:
        expire_days = get(guild_id, "warnings.expire_days")
    cutoff = None
    if expire_days:
        cutoff = now() - timedelta(days=expire_days)
    out = []
    for case in cases(guild_id, target_id=target_id, kind="warn", limit=CASE_CAP):
        if case.get("cleared"):
            continue
        if cutoff is not None:
            try:
                if datetime.fromisoformat(case["at"]) < cutoff:
                    continue
            except (ValueError, KeyError):
                pass
        out.append(case)
    return out


def clear_warnings(guild_id, target_id):
    book = _read(guild_id, CASES, [])
    count = 0
    for case in book:
        if (case.get("target_id") == int(target_id) and case.get("kind") == "warn"
                and not case.get("cleared")):
            case["cleared"] = True
            count += 1
    _write(guild_id, CASES, book)
    return count


# ----- the shelf of saved answers -----

TAGS = "warden_tags.json"
TAG_CAP = 200


def set_tag(guild_id, name, content, author):
    name = (name or "").strip().lower()[:40]
    if not name:
        return False, "a tag needs a name"
    book = _read(guild_id, TAGS, {})
    if len(book) >= TAG_CAP and name not in book:
        return False, f"the shelf is full at {TAG_CAP}"
    book[name] = {"content": str(content)[:1800], "by": author,
                  "at": now().strftime("%Y-%m-%d")}
    _write(guild_id, TAGS, book)
    return True, name


def get_tag(guild_id, name):
    return _read(guild_id, TAGS, {}).get((name or "").strip().lower())


def drop_tag(guild_id, name):
    book = _read(guild_id, TAGS, {})
    if book.pop((name or "").strip().lower(), None) is None:
        return False
    _write(guild_id, TAGS, book)
    return True


def tags(guild_id):
    return _read(guild_id, TAGS, {})


# ---------- templates ----------

def render(template, **values):
    """Fill a template without ever raising. A placeholder nobody passed
    is left standing rather than taking down a welcome message: a greeting
    that reads oddly is a smaller failure than no greeting at all."""
    out = str(template or "")
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out[:1900]
