"""Everything Eugene durably remembers, and what he is allowed to know.

Two shelves, one store. **People**: notes about a particular person, kept
under their id, so that after a month he knows that Horsy argues for sport
and Berri is never awake before noon. **The house**: one shelf for the place
itself -- running jokes, lore, what the server is like.

The house shelf used to be a separate file with a separate cap, separate
ownership rules and a separate pair of tools, in `toolbox.py`. Keeping them
apart quietly broke the promise this module is built on. A memory reading
`[preference] Horsy: prefers a green ball role` is a note about a person by
any reading, but it lived outside the per-person rules, so `forget_about_me`
did not touch it: somebody could ask to be forgotten, be told they had been,
and still be on the other shelf under their own name. One store is what
makes the deletion true, so a strike now sweeps both.

He builds it from ordinary conversation rather than only from messages
aimed at him, which is the only way it could ever be true of the quiet
people. That is a real thing to do to somebody, so three rules hold it:

**A person owns their own entry.** They can read it in full, in their own
words back at them, and strike it. Nobody else can read another person's
entry -- not the cooperative, not an admin, not by asking him nicely.

**Striking means struck.** Someone who deletes their profile is not
someone to start a fresh one on the next pulse; that would make the delete
a formality. So a strike also stops him learning about them until they say
otherwise, and he says so plainly when it happens.

**He only ever learns where he is allowed to speak.** That rule is not
here -- it lives in `may_speak_in` and it is older than this file -- but it
is the reason this one is safe: a house that keeps him to one room has kept
him out of the others entirely, listening included.

Nothing here costs a token and nothing here imports Discord: this module
holds notes and answers questions about them. Deciding what is worth
writing down is the brain's job, and the writing down is clerk.py's.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("people")

# Notes kept per person. Small on purpose: this is a sketch of somebody, not
# a file on them, and the cap is what keeps it a sketch. The oldest goes
# when a new one lands, so it stays a picture of who they are lately.
PROFILE_CAP = 12

# Nobody's note is a paragraph.
NOTE_MAX = 200

# The house shelf sits in the same table under a key no user id can collide
# with -- ids are digits, and this is not. Everything that walks the table
# expecting people has to step over it, which is the whole cost of not
# having a second file.
HOUSE_KEY = "#house"

# Smaller than the old memory book's hundred and fifty, and it is not a cut.
# That cap held both kinds at once, so notes about people crowded out lore
# about the place and the whole book was pasted into every prompt. People
# now have twelve slots each of their own, which is more room than they had;
# what is left competing here is genuinely the house, and sixty is more
# house than this house has.
HOUSE_CAP = 60

# What rides in the prompt, as against what is kept. The book used to send
# its most recent hundred and twenty lines on every message.
HOUSE_SHOWN = 40

# The kinds a memory can be. Only a label for the reader -- nothing branches
# on it -- but it is what makes the shelf skimmable rather than a heap.
KINDS = ("fact", "joke", "preference", "lore")

_path = None


def configure(data: Path):
    global _path
    root = Path(data)
    root.mkdir(parents=True, exist_ok=True)
    _path = root / "people.json"


def _load():
    if _path is None or not _path.exists():
        return {}
    try:
        return json.loads(_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(state):
    if _path is None:
        return
    tmp = _path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(_path)


def _entry(state, user_id):
    return state.get(str(user_id)) or {}


def _words(text):
    """A note as a set of bare words, for telling two notes apart. Case and
    punctuation are noise here: "argues for sport" and "Argues for sport,
    always" are one observation written twice."""
    return {
        word.strip(".,;:!?'\"()[]-") for word in (text or "").lower().split()
    } - {""}


# ---------- what he knows ----------

def note(user_id, display, text, source="observed", now=None):
    """File one note about somebody. Returns why it was refused, or None
    when it was filed -- callers log the reason rather than guessing."""
    text = " ".join((text or "").split())[:NOTE_MAX]
    if not text:
        return "empty"
    state = _load()
    entry = _entry(state, user_id)
    if entry.get("closed"):
        return "they asked him to stop"
    notes = entry.get("notes", [])
    words = _words(text)
    for existing in notes:
        # Not exact-match only: the same observation arrives worded three
        # ways over a fortnight, and a profile that fills up with three
        # spellings of "likes horses" has learned one thing and spent
        # twelve slots on it.
        #
        # By whole words, and never by substring. "quiet" reads as a
        # substring of "quiet in the mornings, loud after midnight", which
        # would let the first note anybody files block every longer one
        # that happens to contain it -- the shortest note wins forever and
        # the profile never grows. One note supersedes another only when it
        # says nothing the other does not.
        other = _words(existing["text"])
        if words <= other or other <= words:
            return "already known"
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    notes.append({"text": text, "at": stamp, "source": source})
    entry["notes"] = notes[-PROFILE_CAP:]
    entry["display"] = display or entry.get("display") or "someone"
    state[str(user_id)] = entry
    _save(state)
    log.info(f"noted about {entry['display']}: {text}")
    return None


# ---------- the house shelf ----------

# What a memory says it is about when it is about the place rather than
# anybody in it. The shelf has no `about` column, so these are dropped and
# every other subject is folded into the sentence instead.
HOUSE_SUBJECTS = ("the server", "the house", "here", "us", "everyone")


def house_line(about, text):
    """One line for the shelf, with its subject kept in the sentence.

    A shelf entry has nowhere to put a subject, so a note filed about
    "Somebody Who Left" would land as "used to run the film nights" -- a
    fact about nobody, and the one word it needed was the one thrown away.
    The generic subjects are the exception: "the server: the kettle vote ran
    for three days" is worse than the sentence on its own.
    """
    about = (about or "").strip()
    text = (text or "").strip()
    if not about or about.lower() in HOUSE_SUBJECTS:
        return text
    return f"{about}: {text}"


def note_house(text, kind="lore", source="observed", now=None):
    """File one thing about the place itself. Same contract as `note`:
    the reason it was refused, or None when it was filed."""
    text = " ".join((text or "").split())[:NOTE_MAX]
    if not text:
        return "empty"
    kind = kind if kind in KINDS else "fact"
    state = _load()
    entry = _entry(state, HOUSE_KEY)
    notes = entry.get("notes", [])
    words = _words(text)
    for existing in notes:
        other = _words(existing["text"])
        if words <= other or other <= words:
            return "already known"
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    notes.append({"text": text, "at": stamp, "source": source, "kind": kind})
    entry["notes"] = notes[-HOUSE_CAP:]
    state[HOUSE_KEY] = entry
    _save(state)
    log.info(f"noted about the house [{kind}]: {text}")
    return None


def house_notes():
    """Everything on the house shelf, oldest first."""
    return list(_entry(_load(), HOUSE_KEY).get("notes", []))


def forget_house(query, whole_words=False):
    """Strike house notes matching a query. Returns how many went.

    Anybody may strike one of these, which is the rule the old book had and
    the right one: a note about the place belongs to the place. A note about
    a *person* is not on this shelf at all -- it is under their id, where
    only they can touch it.

    `whole_words` is for sweeping a name rather than a phrase. A substring
    is what somebody typing "rota" means and is right for `forget`; it is
    badly wrong for a display name, where a member called Al would take
    every note containing "already", "also" or "alright" off the shelf with
    them. Names match as words or not at all.
    """
    query = (query or "").strip().lower()
    if not query:
        return 0
    state = _load()
    entry = _entry(state, HOUSE_KEY)
    notes = entry.get("notes", [])
    if whole_words:
        wanted = _words(query)
        kept = [n for n in notes if not (wanted and wanted <= _words(n["text"]))]
    else:
        kept = [n for n in notes if query not in n["text"].lower()]
    gone = len(notes) - len(kept)
    if gone:
        entry["notes"] = kept
        state[HOUSE_KEY] = entry
        _save(state)
        log.info(f"struck {gone} house note(s) matching {query!r}")
    return gone


def house_book(limit=HOUSE_SHOWN):
    """The house shelf, for the prompt."""
    notes = house_notes()[-limit:]
    if not notes:
        return ""
    lines = "\n".join(
        f"- [{n.get('kind', 'fact')}] {n['text']} ({n['at']})" for n in notes
    )
    return (
        "\n# The house book\n"
        "What you have picked up about this place. A callback in passing, "
        "never a recital, and never read out as a list.\n" + lines + "\n"
    )


def profile(user_id):
    """Everything held about one person, or {} for somebody unknown."""
    return dict(_entry(_load(), user_id))


def summary(user_id):
    """What he knows about somebody, for that somebody to read."""
    entry = _entry(_load(), user_id)
    if entry.get("closed") and not entry.get("notes"):
        return "Nothing. You asked me to stop, and I did."
    notes = entry.get("notes", [])
    if not notes:
        return "Nothing yet."
    lines = "\n".join(f"- {n['text']} *({n['at']})*" for n in notes)
    tail = (
        "\n-# You asked me to stop learning, so this is frozen where it was."
        if entry.get("closed") else
        "\n-# Yours to delete, any time, and I will not start again."
    )
    return lines + tail


def forget_person(user_id, closed=True, display=None):
    """Strike somebody's entry, and their name off the house shelf too.
    Returns how many notes went, both shelves counted.

    `closed` is the part that makes this mean something: a delete that he
    quietly undoes on the next pulse is not a delete. Someone who wants to
    be known again says so, and `reopen` puts it back.

    The house sweep is the other part. While the shelves were two files this
    step did not exist, and a line filed as house lore that happened to name
    somebody survived their deletion -- so he could tell a member their
    notes were gone and then quote one of them back the same evening. The
    match is by name and it is not clever, because the alternative to an
    imperfect sweep here is no sweep at all.
    """
    state = _load()
    entry = _entry(state, user_id)
    gone = len(entry.get("notes", []))
    if not gone and not entry:
        if not closed:
            return 0
    known_as = display or entry.get("display") or "someone"
    state[str(user_id)] = {
        "display": known_as,
        "notes": [],
        "closed": bool(closed),
    }
    _save(state)
    if known_as != "someone":
        gone += forget_house(known_as, whole_words=True)
    log.info(f"struck {gone} note(s) about {known_as}"
             + (" and stopped learning" if closed else ""))
    return gone


def replace_notes(user_id, notes):
    """Put back a filtered list of somebody's own notes. For `forget`, which
    strikes one thing rather than the lot; `forget_person` is the one that
    empties the entry and stops him learning."""
    state = _load()
    entry = _entry(state, user_id)
    if not entry:
        return
    entry["notes"] = list(notes)[-PROFILE_CAP:]
    state[str(user_id)] = entry
    _save(state)


def reopen(user_id):
    """Start learning about somebody again, at their own request."""
    state = _load()
    entry = _entry(state, user_id)
    if not entry.get("closed"):
        return False
    entry["closed"] = False
    state[str(user_id)] = entry
    _save(state)
    return True


def is_closed(user_id):
    return bool(_entry(_load(), user_id).get("closed"))


# ---------- what the brain is shown ----------

def digest(user_ids=(), limit=8):
    """A short who's-who for the prompt: the people in the conversation
    first, and nobody who asked to be left out.

    Deliberately not everybody. The point is that he knows the room he is
    in, and pasting forty profiles into every request would cost real money
    to make him worse at the two people actually talking.
    """
    state = _load()
    wanted = [str(u) for u in user_ids]
    rest = [k for k in state if k not in wanted and k != HOUSE_KEY]
    lines = []
    for key in wanted + rest:
        entry = state.get(key) or {}
        if entry.get("closed") or not entry.get("notes"):
            continue
        notes = "; ".join(n["text"] for n in entry["notes"][-4:])
        lines.append(f"- {entry.get('display', 'someone')}: {notes}")
        if len(lines) >= limit:
            break
    if not lines:
        return ""
    return (
        "\n# People you know\n"
        "What you have picked up about them, for colour and for knowing who "
        "you are talking to. Never read it out as a list, never tell one "
        "person what you know about another, and never use it to guess how "
        "anybody voted.\n" + "\n".join(lines) + "\n"
    )


def counts():
    """(people known, notes held, people who asked to be left out).
    People only -- the house shelf is not a person and never counts as one."""
    state = _load()
    folk = [e for k, e in state.items() if k != HOUSE_KEY]
    known = sum(1 for e in folk if e.get("notes"))
    notes = sum(len(e.get("notes", [])) for e in folk)
    closed = sum(1 for e in folk if e.get("closed"))
    return known, notes, closed


# ---------- the old memory book ----------

def absorb_book(entries, resolve=None):
    """Fold a `clerk_memory.json` into this store, once. Returns how many
    landed on somebody and how many on the house.

    `resolve` turns the old book's free-text `about` into a user id, or None
    when it names nobody here. Without one, everything lands on the house
    shelf: better a note in the wrong place than a note thrown away, and the
    house shelf is the one anybody may strike.
    """
    to_people = to_house = 0
    for e in entries or ():
        if not isinstance(e, dict):
            continue
        text = (e.get("text") or "").strip()
        if not text:
            continue
        about = (e.get("about") or "").strip()
        kind = e.get("kind", "fact")
        source = e.get("source") or "the old memory book"
        user_id = resolve(about) if (resolve and about) else None
        if user_id is not None:
            if note(user_id, about, text, source=source) is None:
                to_people += 1
        else:
            if note_house(house_line(about, text), kind=kind,
                          source=source) is None:
                to_house += 1
    return to_people, to_house
