"""Per-guild settings: everything a server chooses for itself.

The clerk serves one house today and says so in a dozen places. This
module does not. It is keyed by guild id from the first line, so a second
server costs a second directory and nothing else; when the daemon learns
to hold more than one house, this is the part that will not need
rewriting.

Secrets live here, each server bringing its own keys rather than borrowing
the host's, so the store writes 0600 files into a 0700 directory, keeps
keys out of logs, and never hands a key to anything meant for display.
Show a human `fingerprint()`, never the key.

A house may hold keys to more than one annex (Gemini, Grok, Claude) and picks one
to do the talking, so keys are stored per provider and `provider` names
whichever is on duty.

Pure standard library on purpose, and deliberately ignorant of what a
provider is: the store is the one part of the clerk that must be testable
without Discord, a network, or a single third-party package.
"""

import json
import os
import stat
from pathlib import Path

DEFAULT_BUDGET_USD = float(os.environ.get("CLERK_BUDGET_USD", "10"))

_root = None  # set by configure()


def key_field(provider):
    return f"{provider}_api_key"


def model_field(provider):
    return f"{provider}_model"


def configure(data: Path):
    """Point the store at the data directory. Called once at startup."""
    global _root
    _root = Path(data) / "guilds"
    _root.mkdir(parents=True, exist_ok=True)
    _lock_down(_root, stat.S_IRWXU)


def _lock_down(path: Path, mode):
    """Best effort: some hosts mount volumes that refuse chmod."""
    try:
        path.chmod(mode)
    except OSError:
        pass


def home(guild_id) -> Path:
    """The directory holding one server's settings and state."""
    if _root is None:
        raise RuntimeError("settings.configure() was never called")
    path = _root / str(guild_id)
    path.mkdir(parents=True, exist_ok=True)
    _lock_down(path, stat.S_IRWXU)
    return path


def path(guild_id) -> Path:
    """Where a server's settings live, bringing the directory into being.
    For writing; `where()` is the one for looking."""
    return home(guild_id) / "settings.json"


def where(guild_id) -> Path:
    """The same path, without creating anything.

    Reading must not be a write. `load()` used to go through `home()`, so
    merely asking whether a guild had a setting left a directory behind for
    it -- which meant a screen that listed what *might* be cleared quietly
    created a home for every id it asked about, including ones that were
    never a server.
    """
    if _root is None:
        raise RuntimeError("settings.configure() was never called")
    return _root / str(guild_id) / "settings.json"


def installed_guilds():
    """Every guild the host has settings for, newest first is not a thing
    we track; order is arbitrary."""
    if _root is None or not _root.exists():
        return []
    return [int(p.name) for p in _root.iterdir() if p.is_dir() and p.name.isdigit()]


# ---------- reading and writing ----------

def load(guild_id) -> dict:
    p = where(guild_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        # A truncated settings file must not take the whole server down;
        # an unset key is recoverable, a dead clerk is not.
        return {}


def save(guild_id, values: dict):
    """Atomic, and never group- or world-readable: this file holds a key."""
    p = path(guild_id)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(values, indent=2, sort_keys=True))
    _lock_down(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, p)


def get(guild_id, key, default=None):
    return load(guild_id).get(key, default)


# Bumped on every write, and read by anything holding a cached answer that
# was true when this server was configured differently. The bug it exists
# for: a tool refused because a feature was off, that refusal kept as
# evidence, and the feature switched back on -- after which the clerk went
# on insisting it was off, correctly, from a record nothing invalidated.
VERSION = "_config_version"


def config_version(guild_id) -> int:
    """How many times this server's configuration has changed."""
    try:
        return int(load(guild_id).get(VERSION, 0))
    except (TypeError, ValueError):
        return 0


def put(guild_id, **values):
    """Merge values in. Passing None for a key removes it.

    Every call moves the version, including one that sets a key back to
    what it already was: the alternative is comparing values here and
    getting it wrong for the one caller whose value is a dict.
    """
    current = load(guild_id)
    for key, value in values.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    try:
        current[VERSION] = int(current.get(VERSION, 0)) + 1
    except (TypeError, ValueError):
        current[VERSION] = 1
    save(guild_id, current)
    return current


def drop(guild_id, *keys):
    return put(guild_id, **{key: None for key in keys})


# ---------- state files, scoped to the server that owns them ----------

def state_file(guild_id, name, legacy_root: Path = None) -> Path:
    """A per-guild state file. If `legacy_root` holds a file of the same
    name from the single-server era, adopt it once so an upgrade in place
    keeps its history instead of starting from zero."""
    p = home(guild_id) / name
    if not p.exists() and legacy_root is not None:
        old = Path(legacy_root) / name
        if old.exists():
            try:
                os.replace(old, p)
            except OSError:
                return old
    return p


def peek(guild_id, name) -> Path:
    """Where a per-guild file would be, without bringing it into being.

    `home()` creates the directory, which is right when something is about
    to be written and wrong when something is only being looked at:
    `slate.has_history()` asked a harmless question about guild 0 and left
    a `guilds/0/` behind it. Returns None when there is nothing there.
    """
    if _root is None:
        return None
    found = _root / str(guild_id) / name
    return found if found.exists() else None


# ---------- the brains' keys ----------

def fingerprint(secret) -> str:
    """What you may safely show a human: enough to tell two keys apart,
    not enough to use one."""
    if not secret:
        return "none"
    return f"…{secret[-4:]}" if len(secret) > 8 else "…" + "•" * len(secret)


def brain_key(guild_id, provider_name):
    return load(guild_id).get(key_field(provider_name)) or None


def set_brain_key(guild_id, provider_name, key, by=None, at=None):
    """Storing a key also puts that annex on duty: someone who has just
    typed one in wants it used."""
    put(guild_id, **{
        key_field(provider_name): key.strip(),
        f"{provider_name}_key_set_by": by,
        f"{provider_name}_key_set_at": at,
        "provider": provider_name,
    })


def clear_brain_key(guild_id, provider_name):
    drop(
        guild_id,
        key_field(provider_name),
        f"{provider_name}_key_set_by",
        f"{provider_name}_key_set_at",
    )
    if get(guild_id, "provider") == provider_name:
        drop(guild_id, "provider")


def keyed_providers(guild_id, known):
    """Which of `known` this server holds a key for, in the given order."""
    stored = load(guild_id)
    return [name for name in known if stored.get(key_field(name))]


def provider(guild_id, known):
    """The annex on duty: the chosen one if it still has a key, else
    whichever does, else None. Falling back rather than going silent
    means deleting one key of two never mutes the clerk by surprise."""
    keyed = keyed_providers(guild_id, known)
    if not keyed:
        return None
    chosen = get(guild_id, "provider")
    return chosen if chosen in keyed else keyed[0]


def set_provider(guild_id, name):
    put(guild_id, provider=name)


def model(guild_id, provider_name, default=""):
    return get(guild_id, model_field(provider_name)) or default


def set_model(guild_id, provider_name, name):
    put(guild_id, **{model_field(provider_name): name})


def budget_usd(guild_id):
    value = get(guild_id, "budget_usd")
    return float(value) if value is not None else DEFAULT_BUDGET_USD


# ---------- the numbers a house votes by ----------
# The shape of a vote stays in code; the numbers do not. Every one of these
# was a constant, which meant a parliament could pass a decision changing a
# threshold and still need somebody to push a commit before it was true.
#
# Stored under one key, so a server's overrides read as one object and a
# number nobody has touched keeps following the default rather than being
# frozen at whatever it was the day they changed something else.
VOTING = "voting"

# name -> (default, low, high, cast). The bounds are not taste: each one is
# the range in which the rest of the machinery still means what it says. A
# share above 1 is a threshold that can never be met, a window of zero is a
# vote that closes before anybody sees it, and a quorum of 0 makes a public
# poll decidable by its own author alone.
VOTING_RULES = {
    "floor_hours":        (48.0, 0.01, 24 * 30.0, float),
    "removal_hours":      (72.0, 0.01, 24 * 30.0, float),
    "fundamental_share":  (0.75, 0.5, 1.0, float),
    "invite_share":       (0.5, 0.5, 1.0, float),
    "kick_min_yes":       (3, 1, 100, int),
    "away_days":          (14, 1, 365, int),
    "veto_hours":         (24.0, 0.01, 24 * 30.0, float),
    "invite_vetoes":      (1, 1, 100, int),
    "proposal_vetoes":    (1, 1, 100, int),
}

# How each number reads to somebody who has to decide whether to change it.
# Here rather than in clerk.py because two readers want it now -- the panel
# and the tool that changes one by conversation -- and a second copy is how
# the wording and the bounds drift apart.
VOTING_HELP = {
    "floor_hours": "how long an ordinary vote stays open if nothing settles it",
    "removal_hours": "the same, for a removal",
    "fundamental_share": "the share of the roster a removal or a rule change needs",
    "invite_share": "the share of the roster an invitation needs (0.5 is a plain majority)",
    "kick_min_yes": "the fewest yes votes a removal can ever pass on",
    "away_days": "a quiet spell this long takes you out of the count",
    "veto_hours": "how long after a proposal carries it can still be vetoed",
    "invite_vetoes": "how many vetoes overturn a passed invitation",
    "proposal_vetoes": "how many overturn any other passed proposal",
}

# The switches a house votes by, as against the numbers. Same store, same
# door, and they are here rather than beside the feature switches in
# modules.py because they are not features: they are the shape of a vote,
# and they belong with the thresholds they modify.
#
# The invitation is the one proposal whose cost falls on everybody and
# whose benefit is usually one person's, which is why the door is the only
# one that comes with a last word attached. Everything else starts without
# one: a veto over every proposal is a permanent hold for whoever wants it
# most, and a house should have to ask for that on purpose.
VOTING_FLAGS = {
    "invite_veto": True,
    "proposal_veto": False,
    "veto_anonymous": False,
}

VOTING_FLAG_HELP = {
    "invite_veto": "whether a passed invitation can still be vetoed",
    "proposal_veto": "the same, for every other kind of proposal",
    "veto_anonymous": "whether a veto is cast without naming who cast it",
}

# What counts as on and off when somebody types one rather than pressing it.
TRUE_WORDS = {"1", "on", "true", "yes", "y", "enable", "enabled"}
FALSE_WORDS = {"0", "off", "false", "no", "n", "disable", "disabled"}


def is_flag(name):
    return name in VOTING_FLAGS


def known_voting(name):
    """Whether this is something a house may set at all."""
    return name in VOTING_RULES or name in VOTING_FLAGS


_voting_defaults = {
    **{name: rule[0] for name, rule in VOTING_RULES.items()},
    **VOTING_FLAGS,
}


def configure_voting(**defaults):
    """Let the host move a default before any server has an opinion. Used
    for the one number that already had a home outside this module --
    `floor_hours` in server_config.yaml, which CONTRIBUTING tells people to
    wind down to seconds for a sandbox run."""
    for name, value in defaults.items():
        if value is None or not known_voting(name):
            continue
        _voting_defaults[name] = clamp_voting(name, value)


def as_flag(value):
    """A switch, however it was typed. None when it is neither, so a caller
    can refuse it by name rather than reading "maybe" as off."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    word = str(value).strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    return None


def clamp_voting(name, value):
    """One setting, coerced and held inside its bounds. Returns None if it
    is not one at all, so a caller can say so rather than storing a
    string where a threshold goes."""
    if is_flag(name):
        return as_flag(value)
    default, low, high, cast = VOTING_RULES[name]
    try:
        value = cast(value)
    except (TypeError, ValueError):
        return None
    return max(low, min(high, value))


def voting(guild_id=None) -> dict:
    """Every number and switch this house votes by: the defaults, with
    whatever the house has chosen laid over them. Never partial -- a caller
    reads a complete set or none of the numbers would be safe to use."""
    numbers = dict(_voting_defaults)
    if guild_id is None:
        return numbers
    stored = get(guild_id, VOTING) or {}
    for name, value in stored.items():
        if known_voting(name):
            held = clamp_voting(name, value)
            if held is not None:
                numbers[name] = held
    return numbers


def set_voting(guild_id, **values):
    """Store one or more numbers. Returns (accepted, rejected): a name that
    is not a governance number, or a value that is not one, comes back
    named rather than silently ignored.

    Passing None for a number puts it back on the default instead of
    pinning it, so a house that changes its mind is not left holding a copy
    of a value it never chose."""
    stored = dict(get(guild_id, VOTING) or {})
    accepted, rejected = {}, []
    for name, value in values.items():
        if not known_voting(name):
            rejected.append(name)
            continue
        if value is None:
            stored.pop(name, None)
            accepted[name] = _voting_defaults[name]
            continue
        held = clamp_voting(name, value)
        if held is None:
            rejected.append(name)
            continue
        stored[name] = held
        accepted[name] = held
    put(guild_id, **{VOTING: stored or None})
    return accepted, rejected


def voting_overrides(guild_id) -> dict:
    """Only what this house has actually chosen, for saying out loud which
    numbers are theirs and which are simply the ones he came with."""
    stored = get(guild_id, VOTING) or {}
    return {k: v for k, v in stored.items() if known_voting(k)}


# The environment variables a pre-settings host may still be carrying,
# read once each so an upgrade in place does not go quiet.
ENV_KEYS = {
    "gemini": ("GEMINI_API_KEY",),
    "grok": ("XAI_API_KEY", "GROK_API_KEY"),
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
}


def adopt_env_keys(guild_id):
    """One-time upgrade path. The host used to keep a single key in the
    environment for the one server it served; any such key becomes that
    server's own, unless it has already been given one. Returns the
    provider names adopted."""
    chosen_before = get(guild_id, "provider")
    adopted = []
    for name, variables in ENV_KEYS.items():
        if brain_key(guild_id, name):
            continue
        inherited = next(
            (os.environ.get(v, "").strip() for v in variables if os.environ.get(v, "").strip()),
            "",
        )
        if inherited:
            set_brain_key(guild_id, name, inherited, by="the host environment")
            adopted.append(name)
    # set_brain_key puts each adopted annex on duty in turn; an existing
    # choice outranks an inherited one, and two inherited keys go in the
    # order declared above rather than whichever was written last.
    if adopted:
        put(guild_id, provider=chosen_before or adopted[0])
    return adopted
