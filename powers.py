"""The small hands: who is meant, and how this server is configured.

What this file used to be was the other kind of hands entirely -- timeouts,
bans, sweeps, locked rooms, an automod, a warning curve, and a desk where an
administrator signed for the heavy half. All of it is gone, and none of it is
missed: Discord ships AutoMod, every general-purpose bot does warnings and
cases better, and a governance bot that also holds a ban command is a way
around the ballot rather than a feature.

What is left is what governance actually needs. Working out which member
somebody meant, because a removal has to name one. And the two tools that
configure the clerk himself -- the numbers this house votes by, and which
features are switched on -- which belong to no feature on purpose: switching
everything off must still leave the way to switch something back on.
"""

import json
import logging

import bindings
import modules
import settings

log = logging.getLogger("powers")

_deps = {}  # injected by clerk.py

# What counts as yes when somebody types it rather than presses it.
TRUE_WORDS = {"1", "on", "true", "yes", "y", "enable", "enabled"}


def configure(bot, in_cooperative, health_log=None):
    _deps.update(bot=bot, in_cooperative=in_cooperative, health_log=health_log)


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

# ---------- the numbers this house votes by ----------
#
# These used to be a page and a half of moderation settings -- filters,
# warning curves, welcome messages, what gets logged -- and every one of
# them left with the feature that read it. What a server can still change
# by asking is the thing a parliament is actually supposed to legislate:
# how long a vote stays open, what share carries it, when somebody stops
# being counted.

def shown(name, value):
    """One setting as a person reads it. A switch is on or off; a number
    that happens to be stored as 1.0 is 1."""
    if settings.is_flag(name):
        return "on" if value else "off"
    return f"{value:g}" if isinstance(value, float) else str(value)


# What flipping one actually means, said at the moment somebody flips it.
# A veto reaches backwards -- into windows that are open right now -- and
# the two counting switches reach sideways, into ballots people are part
# way through. Either way somebody switching one should be told, rather
# than finding out from a button that stopped working or a threshold that
# moved under a vote they had already cast in.
#
# It lives here rather than in clerk.py because both doors want it now: the
# steward's panel and the tool that changes one by conversation. Told once
# it is a promise about what the code does; told twice it is two promises
# that will stop agreeing.
SWITCH_TAIL = {
    "invite_veto": "It reaches the windows already open, so an invitation "
                   "that carried in the last few hours is covered by "
                   "whatever this now says.",
    "proposal_veto": "It reaches the windows already open, so a proposal "
                     "that carried in the last few hours is covered by "
                     "whatever this now says.",
    "veto_anonymous": "It applies to vetoes cast from now on. One already "
                      "cast keeps the rule it was cast under.",
    "count_turnout": "It reaches the votes already open, and it is the rule "
                     "that lets a vote end early: counted against turnout, "
                     "most of them now run to the clock instead.",
    "abstain_steps_out": "It reaches the votes already open, so an "
                         "abstention already cast counts by whatever this "
                         "now says.",
}

WINDOW_TAIL = ("Votes already on the floor keep the window they were filed "
               "with; thresholds are worked out fresh, so those move now.")
LIVE_TAIL = ("It applies to every vote from this moment, including the ones "
             "already open.")


def reaches(name):
    """How far back a change to this one goes.

    The window is the one number a vote carries a copy of, so moving it
    leaves everything already filed where it was; everything else is worked
    out at the moment it is needed and therefore moves under votes people
    have already cast in. Somebody changing one is owed that distinction
    whichever door they came through.
    """
    if name in ("floor_hours", "removal_hours"):
        return WINDOW_TAIL
    return SWITCH_TAIL.get(name, LIVE_TAIL)


# The word the panel's value box takes for "put it back". Typed here too,
# so the two doors answer to the same thing and nobody is told to press a
# button to undo what they were allowed to ask for.
DEFAULT_WORDS = {"default", "defaults", "reset", "unset", ""}


def _typed(key, value):
    """A value however it arrived at the tool.

    A switch and a number come through one field, so that field is a string
    and everything reaches settings.py as text. int("3.0") raises, which
    would have come back as a bounds refusal for a number well inside its
    bounds -- so anything numeric is a float first and settings.py casts it
    the rest of the way.
    """
    if settings.is_flag(key):
        return value  # settings.as_flag reads the words
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return value
    return value


async def _announce(guild, line):
    """The same line the panel writes when somebody presses instead of asks.

    Guarded because the setting is already stored by the time this runs: a
    host whose logging is broken should not have a change land and then be
    reported to the member as a failure.
    """
    write = _deps.get("health_log")
    if write is None:
        return
    try:
        await write(guild, line)
    except Exception as e:
        log.warning(f"could not log a settings change: {e!r}")


async def act_settings(guild, invoker, args):
    """Every number and switch, what it is set to, what it means, and --
    for a number -- its bounds."""
    held = settings.voting(guild.id)
    chosen = settings.voting_overrides(guild.id)
    rows = {}
    for name, blurb in settings.VOTING_HELP.items():
        default, low, high, _cast = settings.VOTING_RULES[name]
        rows[name] = {
            "now": held[name],
            "what": blurb,
            "default": default,
            "between": [low, high],
            "theirs": name in chosen,
        }
    for name, blurb in settings.VOTING_FLAG_HELP.items():
        rows[name] = {
            "now": held[name],
            "what": blurb,
            "default": settings.VOTING_FLAGS[name],
            "on_or_off": True,
            "theirs": name in chosen,
        }
    return _ok(
        settings=rows,
        note="a vote ends when its result can no longer change, so the "
             "window is a backstop rather than the rule"
             + (" -- except where a share is counted against turnout, "
                "which mostly puts a vote back on its window, because "
                "every ballot still to come moves the bar"
                if held["count_turnout"] else ""),
    )


def _no_such(key):
    near = [k for k in settings.VOTING_HELP if key.lower() in k][:4]
    near += [k for k in settings.VOTING_FLAG_HELP if key.lower() in k][:4]
    return _err(f"{key!r} is not a setting." +
                (f" Did you mean: {', '.join(near)}?" if near else
                 " Call list_settings."))


async def act_set_setting(guild, invoker, args):
    """Change one, or put it back where the word for it is the value.

    The bounds are settings.py's, not this handler's: a number outside them
    is pulled to the nearest it can be, and one that is not a number at all
    is refused. Both come back saying what the range was, because a value
    quietly held at 100 reads exactly like a value that was granted.
    """
    key = str(args.get("key") or "").strip()
    if not settings.known_voting(key):
        return _no_such(key)
    raw = args.get("value")
    if raw is None:
        return _err(f"what should {key} be? a number, or on or off if it is "
                    f"a switch")
    # A missing value is the question above; the word for the default is a
    # deliberate one, and the two must never be the same thing -- a model
    # that forgets the argument would otherwise clear the house's choice.
    wanted = (None if str(raw).strip().lower() in DEFAULT_WORDS
              else _typed(key, raw))
    before = settings.voting(guild.id)[key]
    held, rejected = settings.set_voting(guild.id, **{key: wanted})
    if rejected:
        if settings.is_flag(key):
            return _err(f"{key} is on or off, nothing else")
        _default, low, high, _cast = settings.VOTING_RULES[key]
        return _err(f"{key} has to be a number between {low} and {high}, "
                    f"or 'default' to put it back")
    now = held[key]
    what = (settings.VOTING_FLAG_HELP.get(key)
            or settings.VOTING_HELP.get(key, ""))
    # Say so when a value was pulled inside its bounds, and say what the
    # bounds were. settings.py clamps rather than refuses, so without this
    # a request for a fortnight-long veto window comes back reading exactly
    # like a request that was granted.
    pulled = None
    if wanted is not None and not settings.is_flag(key):
        _default, low, high, cast = settings.VOTING_RULES[key]
        try:
            if cast(wanted) != now:
                pulled = (f"{key} lives between {low} and {high}, so it was "
                          f"held at the nearest it can be")
        except (TypeError, ValueError):
            pulled = None
    await _announce(guild, f"⚙️ `{key}` {shown(key, before)} → "
                           f"{shown(key, now)}, by "
                           f"{getattr(invoker, 'display_name', '?')}.")
    log.info(f"guild {guild.id}: {key} {before} -> {now} "
             f"by {getattr(invoker, 'display_name', '?')}")
    return _ok(done="back to the default" if wanted is None else "set",
               key=key, was=before, now=now, what=what, held=pulled,
               reaches=reaches(key))


async def act_reset_settings(guild, invoker, args):
    """Back to the defaults, and say which were theirs to begin with.

    Named or all. Putting one number back used to mean clearing every other
    choice the house had made beside it, so undoing one change meant undoing
    five they still wanted -- which is not an undo anybody uses.
    """
    theirs = settings.voting_overrides(guild.id)
    asked = args.get("keys")
    if isinstance(asked, str):
        asked = [asked]
    names = [str(k).strip() for k in asked] if asked else list(theirs)
    for key in names:
        if not settings.known_voting(key):
            return _no_such(key)
    was = settings.voting(guild.id)
    settings.set_voting(guild.id, **{k: None for k in names})
    now = settings.voting(guild.id)
    cleared = [k for k in names if k in theirs]
    who = getattr(invoker, "display_name", "?")
    for key in cleared:
        await _announce(guild, f"⚙️ `{key}` {shown(key, was[key])} → "
                               f"{shown(key, now[key])}, back to the "
                               f"default, by {who}.")
    if cleared:
        log.info(f"guild {guild.id}: {', '.join(cleared)} back to default "
                 f"by {who}")
    return _ok(done="reset", keys_cleared=cleared,
               now={k: now[k] for k in cleared},
               already_default=[k for k in names if k not in theirs],
               reaches=list(dict.fromkeys(reaches(k) for k in cleared)))


# ---------- which features are on ----------

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
        on = on.strip().lower() in TRUE_WORDS
    was = modules.enabled(guild.id, key)
    changed, knock_on = modules.set_enabled(guild.id, key, bool(on))
    name = modules.name(key)
    if not changed and was == bool(on):
        return _ok(feature=key, on=was, note=f"{name} was already "
                   + ("on" if was else "off"))
    log.info(f"guild {guild.id}: {key} -> {'on' if on else 'off'} "
             f"by {getattr(invoker, 'display_name', '?')}")
    await _announce(
        guild,
        f"⚙️ {name} switched {'on' if on else 'off'} by "
        f"{getattr(invoker, 'display_name', '?')}"
        + (", and with it " + ", ".join(modules.name(k) for k in knock_on)
           if knock_on else "") + ".",
    )
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
    "list_settings": act_settings,
    "set_setting": act_set_setting,
    "reset_settings": act_reset_settings,
}
