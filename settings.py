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
    return home(guild_id) / "settings.json"


def installed_guilds():
    """Every guild the host has settings for, newest first is not a thing
    we track; order is arbitrary."""
    if _root is None or not _root.exists():
        return []
    return [int(p.name) for p in _root.iterdir() if p.is_dir() and p.name.isdigit()]


# ---------- reading and writing ----------

def load(guild_id) -> dict:
    p = path(guild_id)
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


def put(guild_id, **values):
    """Merge values in. Passing None for a key removes it."""
    current = load(guild_id)
    for key, value in values.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
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
