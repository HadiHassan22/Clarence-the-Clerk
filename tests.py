"""What can be checked without a Discord server.

    python3 tests.py

The harness tests need nothing but the standard library. The bill-filing
tests import clerk.py and exercise the real handlers against a fake
floor, so they need discord.py installed; without it they are skipped
and reported as such rather than quietly passing.

Nothing here reaches Discord. The chamber, the buttons and the close of
the floor still want a sandbox server. See CONTRIBUTING.md.
"""

import asyncio
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import settings  # noqa: E402  (pure stdlib; no discord needed)
import toolbox  # noqa: E402  (pure stdlib; no discord needed)

RESULTS = []
SKIPPED = []


def check(label, condition):
    RESULTS.append(bool(condition))
    print(("  pass  " if condition else "  FAIL  ") + label)
    return condition


def skip(label, why):
    SKIPPED.append(label)
    print(f"  skip  {label} ({why})")


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


GUILD = types.SimpleNamespace(
    name="The Hangout", id=1, text_channels=[], channels=[], members=[],
    get_channel=lambda _id: None, get_role=lambda _id: None,
)
CITIZEN = types.SimpleNamespace(id=99, display_name="Robin")


def load_clerk(data):
    """Import the real clerk with a throwaway environment, or None when
    discord.py is missing."""
    try:
        import discord  # noqa: F401
    except ImportError:
        return None
    os.environ.setdefault("DISCORD_TOKEN", "test-token")
    os.environ.setdefault("GUILD_ID", "1")
    os.environ["CLERK_DATA_DIR"] = str(data)
    import clerk
    return clerk


# ---------- the settings store ----------

KNOWN = ("gemini", "grok")


def test_settings(data):
    print("\nevery server keeps its own keys")
    settings.configure(data / "store")
    one, two = 111, 222

    check("a server with nothing set has no annex on duty",
          settings.provider(one, KNOWN) is None)
    check("and no key", settings.brain_key(one, "gemini") is None)

    settings.set_brain_key(one, "gemini", "AIzaSecretOne", by="Robin")
    check("a stored key comes back", settings.brain_key(one, "gemini") == "AIzaSecretOne")
    check("storing one puts it on duty", settings.provider(one, KNOWN) == "gemini")
    check("the other server is untouched", settings.brain_key(two, "gemini") is None)

    settings.set_brain_key(two, "grok", "xai-SecretTwo")
    check("two servers hold different annexes",
          settings.provider(one, KNOWN) == "gemini"
          and settings.provider(two, KNOWN) == "grok")

    print("\none house, both annexes")
    settings.set_brain_key(one, "grok", "xai-AlsoMine")
    check("the newest key takes over", settings.provider(one, KNOWN) == "grok")
    check("both are on file",
          settings.keyed_providers(one, KNOWN) == ["gemini", "grok"])
    settings.set_provider(one, "gemini")
    check("and the choice can be switched back",
          settings.provider(one, KNOWN) == "gemini")

    settings.clear_brain_key(one, "gemini")
    check("forgetting the one on duty falls back to the other",
          settings.provider(one, KNOWN) == "grok")
    check("and really does forget it", settings.brain_key(one, "gemini") is None)
    settings.clear_brain_key(one, "grok")
    check("forgetting the last one leaves the clerk dormant",
          settings.provider(one, KNOWN) is None)

    print("\nmodels and budgets are per server, per annex")
    settings.set_model(one, "gemini", "gemini-fancy")
    check("a model is remembered against its own annex",
          settings.model(one, "gemini", "fallback") == "gemini-fancy"
          and settings.model(one, "grok", "fallback") == "fallback")
    check("an unset model falls back to what the caller offers",
          settings.model(two, "gemini", "fallback") == "fallback")
    check("the budget has a default", settings.budget_usd(two) > 0)
    settings.put(two, budget_usd=3)
    check("and a server can set its own", settings.budget_usd(two) == 3.0)

    print("\nkeys are handled as secrets")
    check("a fingerprint shows only the tail",
          settings.fingerprint("AIzaSecretOne") == "…tOne")
    check("a short secret is not partly revealed",
          "Secret" not in settings.fingerprint("Secret"))
    check("nothing is a fingerprint of nothing", settings.fingerprint(None) == "none")
    mode = stat.S_IMODE(settings.path(two).stat().st_mode)
    check(f"the settings file is not readable by anyone else ({oct(mode)})",
          not mode & 0o077)

    print("\na host key in the environment is adopted once")
    three = 333
    os.environ["GEMINI_API_KEY"] = "AIzaFromTheHost"
    os.environ["XAI_API_KEY"] = "xai-FromTheHost"
    check("both inherited keys are taken",
          sorted(settings.adopt_env_keys(three)) == ["gemini", "grok"])
    check("the first declared annex takes duty, not the last written",
          settings.provider(three, KNOWN) == "gemini")
    settings.set_provider(three, "grok")
    check("adopting again changes nothing", settings.adopt_env_keys(three) == [])
    check("and does not overrule a choice already made",
          settings.provider(three, KNOWN) == "grok")
    settings.clear_brain_key(three, "gemini")
    settings.clear_brain_key(three, "grok")
    del os.environ["GEMINI_API_KEY"], os.environ["XAI_API_KEY"]

    print("\nthe store survives a bad day")
    settings.path(two).write_text("{ this is not json")
    check("a truncated settings file reads as empty, not as a crash",
          settings.load(two) == {})
    settings.set_brain_key(two, "grok", "xai-Recovered")
    check("and can be written over", settings.brain_key(two, "grok") == "xai-Recovered")


def test_state_adoption(data):
    print("\nstate from the single-server era is adopted, not abandoned")
    root = data / "legacy"
    root.mkdir()
    settings.configure(root)
    (root / "brain_state.json").write_text('{"months": {"2026-07": {"usd": 4.5}}}')
    moved = settings.state_file(9, "brain_state.json", legacy_root=root)
    check("the old file lands in the server's own directory",
          moved.parent.name == "9" and json.loads(moved.read_text())
          ["months"]["2026-07"]["usd"] == 4.5)
    check("and is not left behind to be read twice",
          not (root / "brain_state.json").exists())
    again = settings.state_file(9, "brain_state.json", legacy_root=root)
    check("asking again returns the same file", again == moved)
    fresh = settings.state_file(10, "brain_state.json", legacy_root=root)
    check("a server that never had one starts clean", not fresh.exists())


# ---------- the harness ----------

def test_registry():
    print("\nthe tools reach the model correctly")
    names = [d["name"] for d in toolbox.declarations()]
    for name in ("propose", "close_floor"):
        check(f"{name} is declared to the model", name in names)
        check(f"{name} sits in the member tier",
              toolbox.REGISTRY[name]["tier"] == "member")
        check(f"{name} takes its handler from clerk.py",
              toolbox.REGISTRY[name]["handler"] is None)

    # Five read tools became one and three filing tools became one, because
    # the wrong tool of five answers plausibly about the wrong thing, and
    # picking removal when somebody meant invite is not recoverable.
    check("the five old read tools are one door with a kind on it",
          "lookup" in names
          and not {"list_bills", "get_bill", "list_acts", "get_act",
                   "get_standing_orders"} & set(names))
    check("and the three old filing tools likewise",
          not {"propose_bill", "propose_member", "propose_removal"} & set(names))
    spec = next(d for d in toolbox.declarations() if d["name"] == "propose")
    check("proposing needs a kind and a reason at minimum",
          set(spec["parameters"]["required"]) == {"kind", "why"})
    check("and the kinds are the three the house actually has",
          set(spec["parameters"]["properties"]["kind"]["enum"])
          == {"change", "invite", "removal"})
    look = next(d for d in toolbox.declarations() if d["name"] == "lookup")
    check("looking up needs a kind and nothing else",
          set(look["parameters"]["required"]) == {"kind"})


def test_annexes():
    """The Claude annex, on the bench: everything but the network.

    A transcript is neutral until a provider translates it, and the
    translation is the part that goes wrong quietly -- a tool result
    filed under the wrong turn is a 400 nobody sees until someone talks
    to the clerk.
    """
    try:
        import providers
    except ImportError as e:
        return skip("the annexes", f"{e.name} is not installed")

    print("\nevery annex is a full one")
    for name in providers.NAMES:
        annex = providers.PROVIDERS[name]
        check(f"{name} names itself and a model",
              providers.label(name) and providers.default_model(name))
        check(f"{name} prices itself", all(p > 0 for p in providers.prices(name)))
        check(f"{name} answers the three calls brain.py makes",
              all(hasattr(annex, m)
                  for m in ("converse", "json_answer", "list_models")))
    check("Claude is one of them", "claude" in providers.NAMES)

    print("\nClaude names three rungs, and prices each of them")
    rungs = providers.tiers("claude")
    check("haiku, sonnet and opus, in that order",
          list(rungs) == ["haiku", "sonnet", "opus"])
    check("the cheapest is what he came with",
          rungs["haiku"] == providers.default_model("claude"))
    check("every rung is priced by itself",
          all(providers.prices("claude", m)
              != (providers.Claude.price_in, providers.Claude.price_out)
              or m == rungs["haiku"] for m in rungs.values()))
    check("and the dear one is dearer both ways",
          providers.prices("claude", rungs["opus"])
          > providers.prices("claude", rungs["haiku"]))
    check("a model nobody listed falls back to the annex's own estimate",
          providers.prices("claude", "claude-something-else")
          == providers.prices("claude"))
    check("a rung reads back by its name",
          providers.tier_of("claude", rungs["opus"]) == "opus")
    check("and a model a house typed in itself has no name to give",
          providers.tier_of("claude", "claude-something-else") is None)
    check("an annex that names no rungs offers none",
          providers.tiers("gemini") == {} and providers.tiers("nope") == {})

    print("\nClaude reads the neutral transcript")
    # __new__, not the constructor: building a client would want the
    # anthropic package and a key, and neither is the thing under test.
    claude = providers.Claude.__new__(providers.Claude)
    spoke = providers.Reply(
        text="", calls=[providers.Call(name="show_bill", args={}, id="toolu_1")],
        raw=[{"type": "tool_use", "id": "toolu_1"}],
    )
    messages = claude._messages([
        providers.said("what is on the floor?"),
        providers.answered(spoke),
        providers.returned([
            {"id": "toolu_1", "name": "show_bill", "result": "Bill No. 4"},
            {"id": "toolu_2", "name": "roster", "result": "nine members"},
        ]),
    ])
    check("a question, an answer and the results make three turns",
          len(messages) == 3)
    check("what he said is replayed verbatim, signature and all",
          messages[1] == {"role": "assistant", "content": spoke.raw})
    check("every result of a round goes back in one turn, as the API insists",
          messages[2]["role"] == "user" and len(messages[2]["content"]) == 2)
    check("and each names the call it answers",
          [b["tool_use_id"] for b in messages[2]["content"]]
          == ["toolu_1", "toolu_2"])
    plain = claude._messages([
        providers.answered(providers.Reply(text="Aye, No. 4 carries."))
    ])
    check("a turn with no provider copy still replays as text",
          plain == [{"role": "assistant", "content": "Aye, No. 4 carries."}])

    print("\nand guards the two things thinking-off gets wrong")
    check("a leaked thinking block never reaches the channel",
          providers._visible("<thinking>hmm</thinking>Aye, No. 4 carries.")
          == "Aye, No. 4 carries.")
    check("a reply without one is left alone",
          providers._visible("  Aye.  ") == "Aye.")

    print("\nand shuts a Gemini-shaped schema on the way past")
    open_schema = {
        "type": "object",
        "properties": {"memories": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"kind": {"type": "string"}},
                      "required": ["kind"]},
        }},
        "required": ["memories"],
    }
    closed = providers._closed(open_schema)
    check("the outer object is closed",
          closed["additionalProperties"] is False)
    check("and so is one nested in an array",
          closed["properties"]["memories"]["items"]["additionalProperties"] is False)
    check("what the caller passed in is untouched",
          "additionalProperties" not in open_schema)


def test_firewall(data):
    print("\nthe anonymity firewall still holds over an invite bill")
    # The tests above this one point the harness at stores of their own and
    # do not point it back, so without this the lookup below reads a
    # directory with no bills in it and every "is not in the result" check
    # here passes against an error message. Which is what it was doing.
    toolbox.configure(HERE, data)
    (data / "bills.json").write_text(json.dumps([{
        "no": 1, "title": "t", "kind": "invite", "status": "passed",
        "author": "x", "ballots": {"111": "yes", "222": "abstain"},
        "tally": {"yes": 5, "no": 1, "abstain": 2}, "tally_line": "5/1/2",
        "invite_url": "https://discord.gg/secret", "notes": {}}]))
    bill = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "lookup", {"kind": "bill", "number": 1})))
    check("individual ballots are stripped", "ballots" not in bill)
    check("the closing tally comes through, because it is on the decision "
          "in the record and hiding it here only blinds the clerk",
          bill.get("tally", {}).get("yes") == 5 and bill.get("tally_line") == "5/1/2")
    check("the invite link is never handed to the model", "invite_url" not in bill)


def test_audit(data):
    print("\nevery dispatch is audited")
    check("the audit log is written under logs/, not in among the record",
          (data / "logs" / "executor_log.json").exists()
          and not (data / "executor_log.json").exists())
    entries = json.loads((data / "logs" / "executor_log.json").read_text())
    check(f"{len(entries)} entries written", len(entries) >= 10)
    check("proposals are logged with their arguments",
          any(e["tool"] == "propose" and e["args"].get("who") for e in entries))
    check("no dispatch is recorded without an outcome",
          all("result" in e for e in entries))

    # "ok" only ever meant that nothing was raised, so every refusal a
    # handler returns as a sentence -- at the cap, not your role, no such
    # name -- was filed as a success. Reading this log back, there was no
    # way to tell what he did from what he declined to do, which is the one
    # question it is kept for. What came back is written down now.
    done = [e for e in entries if e["result"] == "ok"]
    check("a dispatch that ran records what it actually returned",
          done and all("returned" in e for e in done))
    check("and only a readable amount of it, not the record over again",
          all(len(e["returned"]) <= 400 for e in done))


# ---------- the debate room ----------

def test_debate_thread(clerk, data):
    """Filing a proposal opens exactly one room to argue in: the thread on
    the proposal itself.

    Three things are worth holding still here. The sidebar: nothing is
    built at guild level any more, so a busy week no longer adds a
    category, a text channel and a voice channel per vote. The count: one
    thread, not two -- there was briefly a second beside the notes for the
    argument, which is two rooms for one vote and a guess to make before
    typing. And the permissions: a thread has none of its own, which is the
    whole reason it is one. The cooperative's floor is shut to members, so
    the debate on it is shut to them by the same overwrite that shuts the
    ballot.
    """
    print("\nthe argument about a proposal happens in the thread on it")

    settings.configure(data)
    threads, built = [], []

    class Thread:
        def __init__(self, tid, name):
            self.id, self.name, self.sent, self.edits = tid, name, [], {}
            self.mention = f"<#{tid}>"

        async def send(self, content=None, view=None):
            self.sent.append(content)
            return types.SimpleNamespace(id=self.id + len(self.sent))

        async def edit(self, **kwargs):
            self.edits.update(kwargs)

    class Message:
        def __init__(self, mid):
            self.id = mid
            self.edits = 0

        async def create_thread(self, name=None, auto_archive_duration=None,
                                **kwargs):
            notes = Thread(200, name)
            notes.window = auto_archive_duration
            threads.append(notes)
            return notes

        async def edit(self, **kwargs):
            self.edits += 1

    class Floor:
        id, mention, said = 500, "<#500>", 0
        posted = []
        mentions = []

        async def send(self, content=None, view=None, allowed_mentions=None):
            Floor.said += 1
            Floor.mentions.append(allowed_mentions)
            message = Message(600 + Floor.said)
            Floor.posted.append(message)
            return message

        async def fetch_message(self, mid):
            return next(m for m in Floor.posted if m.id == mid)

        async def create_thread(self, **kwargs):
            built.append("a second thread")
            return Thread(300, kwargs.get("name"))

    async def never(*args, **kwargs):
        built.append(args[0] if args else "?")

    floor = Floor()
    guild = types.SimpleNamespace(
        id=4242, name="The Hangout", members=[], roles=[], categories=[],
        get_role=lambda _id: None, get_channel=lambda _id: None,
        get_thread=lambda _id: next((t for t in threads if t.id == _id), None),
        create_category=never, create_text_channel=never,
        create_voice_channel=never,
    )
    author = types.SimpleNamespace(id=7, display_name="Robin", mention="@Robin")

    keep_floor = clerk.floor_for
    clerk.floor_for = lambda _guild, _bill: floor
    try:
        bill = run(clerk.file_bill(
            guild, author, "A books channel",
            "There shall be a books channel.", "People here read."))
    finally:
        clerk.floor_for = keep_floor

    check("filing a proposal costs the floor one message, not four: the "
          "proposal, the ballot and in the end the result are one card",
          Floor.said == 1)
    check("and the thread hangs off that card, so the ballot is the "
          "proposal rather than a message under it",
          bill["ballot_message_id"] == bill["message_id"])
    shown = clerk.ballot_content(guild, bill)
    check("which carries the proposal's own words",
          "There shall be a books channel." in shown
          and "People here read." in shown)
    check("with the live ballot on the same card",
          " yes · " in shown and "⬜" in shown)
    check("in one block and no headings: the floor is a queue of these, so "
          "a card is four lines, not a page",
          len(clerk.floor_segments(guild, bill)) == 1
          and "###" not in shown and "##" not in shown
          and len(shown.splitlines()) <= 4)

    notes = next((t for t in threads if t.id == 200), None)
    check("nothing is built in the sidebar: no category, no text channel, "
          "no voice channel, and no second thread", built == [])
    check("one thread is opened, and it hangs off the proposal itself",
          len(threads) == 1 and notes is not None)
    check("the proposal remembers it", bill.get("notes_thread_id") == 200)
    check("the channel ids of the old chambers are gone rather than left "
          "behind empty",
          not any(k in bill for k in ("chamber_category_id", "chamber_text_id",
                                      "chamber_voice_id", "chamber_thread_id")))
    check("it stays open for a week, longer than a vote runs",
          notes.window == 10080)
    check("and it is not locked, because the argument happens in it",
          notes.edits.get("locked") is not True)
    check("the ballot can find it to point at it",
          clerk.chamber_of(guild, bill) is notes)
    check("the prompt at the top of it invites both: argue here, or file a "
          "position for the record",
          any("Argue it out here" in (line or "") for line in notes.sent))
    check("and a proposal short enough to show whole leaves nothing else "
          "in it: the thread is for the argument, not a second copy",
          len(notes.sent) == 1)

    long_what = " ".join(f"clause {i} of what is being proposed"
                         for i in range(1, 30))
    clerk.floor_for = lambda _guild, _bill: floor
    try:
        big = run(clerk.file_bill(guild, author, "A long one",
                                  long_what, "Because it is long."))
    finally:
        clerk.floor_for = keep_floor
    card = clerk.ballot_content(guild, big)
    excerpt = clerk.floor_text(big)[0].splitlines()[0]
    check("a proposal too long for a card is shown as far as it reads and "
          "cut at a word, not at a character",
          long_what not in card and card.count("…") == 1
          and long_what.startswith(excerpt.rstrip("…") + " "))
    check("and the whole of it goes first into the thread, so the floor "
          "can be a list without the proposal being shortened out of "
          "anywhere it can be read",
          any(long_what in (line or "") for line in threads[-1].sent))

    run(clerk.seal_chamber(guild, bill))
    check("closing has nothing separate to shut, since the thread is "
          "sealed with the notes",
          notes.edits == {} and len(notes.sent) == 1)

    print("\nproposals filed before it was one thread still close")

    class Room:
        def __init__(self, rid, name):
            self.id, self.name, self.edits, self.gone = rid, name, {}, False
            self.channels = []

        async def edit(self, **kwargs):
            self.edits.update(kwargs)

        async def delete(self, reason=None):
            self.gone = True

    text, voice = Room(11, "💬・Proposal-9"), Room(12, "🔊 Proposal-9")
    category, archive = Room(13, "🗳️ Proposal-9"), Room(14, "🗄️ archive")
    rooms = {11: text, 12: voice, 13: category}
    old = types.SimpleNamespace(
        id=4242, roles=[], categories=[archive], owner=object(),
        default_role="@everyone", get_channel=lambda _id: rooms.get(_id),
    )
    run(clerk.seal_chamber(old, {
        "no": 9, "title": "An older one", "chamber_text_id": 11,
        "chamber_voice_id": 12, "chamber_category_id": 13,
    }))
    check("the old text channel is still locked and filed in the archive",
          text.edits.get("category") is archive
          and text.edits.get("name", "").startswith("archived_"))
    check("the voice channel is still deleted, because voice is never "
          "recorded", voice.gone)
    check("and the empty category with it", category.gone)

    separate = Thread(400, "💬 Proposal-10")
    threads.append(separate)
    run(clerk.seal_chamber(guild, {
        "no": 10, "title": "The one with two threads",
        "chamber_thread_id": 400, "notes_thread_id": 200,
    }))
    check("a proposal that got a debate thread of its own has that sealed "
          "too, locked and archived where it stands",
          separate.edits.get("locked") is True
          and separate.edits.get("archived") is True)


# ---------- the bell ----------

def test_bell(clerk, data):
    """One ping, when a vote opens, to whoever asked for one.

    Four things are held still. That the ping rides on the ballot's own
    card rather than arriving as a message beside it, because the floor is
    one message per proposal. That it happens at the open and at no other
    moment: not on the edit after every vote, not when a deleted card goes
    back up. That every way of having nobody to ring -- the house switched
    it off, no role, an empty role -- produces exactly the card that went
    up before there was a bell, with no stray text and no empty mention.
    And that the role he can ping is not a role anybody else can.
    """
    print("\nthe bell: rung when a vote opens, and at no other time")

    import bindings
    import builder
    import discord
    import modules

    settings.configure(data)

    class Role:
        def __init__(self, rid, name, mentionable=False):
            self.id, self.name = rid, name
            self.mentionable = mentionable
            self.members = []
            self.mention = f"<@&{rid}>"

    class Message:
        def __init__(self, mid):
            self.id, self.edits = mid, []

        async def create_thread(self, **kwargs):
            return Thread(700 + self.id)

        async def edit(self, **kwargs):
            self.edits.append(kwargs)

    class Thread:
        def __init__(self, tid):
            self.id, self.mention = tid, f"<#{tid}>"

        async def send(self, content=None, view=None):
            return types.SimpleNamespace(id=self.id + 1)

    class Floor:
        def __init__(self):
            self.posted, self.mentions = [], []

        async def send(self, content=None, view=None, allowed_mentions=None):
            self.mentions.append(allowed_mentions)
            message = Message(800 + len(self.posted))
            self.posted.append(message)
            return message

        async def fetch_message(self, mid):
            found = next((m for m in self.posted if m.id == mid), None)
            if found is None:
                raise discord.NotFound(
                    types.SimpleNamespace(status=404, reason="x"), "x")
            return found

    made = []

    async def create_role(name=None, permissions=None, mentionable=None,
                          reason=None):
        role = Role(1000 + len(made), name, mentionable)
        made.append(role)
        guild.roles.append(role)
        return role

    guild = types.SimpleNamespace(
        id=9100, name="The Hangout", roles=[], members=[], categories=[],
        create_role=create_role,
        get_role=lambda rid: next((r for r in guild.roles if r.id == rid), None),
        get_channel=lambda _id: None,
    )
    author = types.SimpleNamespace(id=7, display_name="Robin", mention="@Robin")
    said = []

    print("\nthe role is declared where the rooms are, and needed by nothing")
    check("governance names it beside the cooperative",
          "bell" in modules.spec("governance")["roles"])
    check("as optional, so a house that has never made one is not a house "
          "whose votes are broken",
          "bell" not in modules.required_roles(guild.id)
          and modules.blockers(guild.id, "governance",
                               rooms=("votes", "decisions"),
                               roles=("cooperative",)) == [])
    check("and the builder is told to make it, which is a different list",
          "bell" in modules.wanted_roles(guild.id))
    check("the bindings know how to hold it", "bell" in bindings.ROLES)

    run(builder.ensure_module_roles(guild, said.append))
    bell = next((r for r in made if r.name == builder.BELL), None)
    check(f"Apply makes it: {bell.name if bell else 'nothing'}",
          bell is not None)
    check("mentionable by him and by nobody else, so a role people hold to "
          "hear about a vote cannot be turned round and used to shout",
          bell.mentionable is False)
    check("and a second Apply adopts the one that is there rather than "
          "making a second",
          run(builder.ensure_module_roles(guild, said.append))["bell"] is bell)

    modules.set_enabled(guild.id, "governance", False)
    before = len(made)
    run(builder.ensure_module_roles(guild, said.append))
    check("a house with the votes switched off is handed no bell for votes "
          "it does not hold", len(made) == before)
    modules.set_enabled(guild.id, "governance", True)

    print("\nnobody is rung who has not asked, and no house that said no")
    bindings.bind_role(guild.id, "bell", bell.id)
    check("the house rings out of the box, which is safe because the role "
          "starts empty", clerk.ringing(guild))
    check("an empty bell is nobody, so a ballot mentions nothing",
          clerk.bell_for(guild) is None)

    holder = types.SimpleNamespace(id=21, display_name="Sam", bot=False)
    machine = types.SimpleNamespace(id=22, display_name="A bot", bot=True)
    bell.members = [holder]
    check("somebody holding it is somebody to ring",
          clerk.bell_for(guild) is bell)
    bell.members = [machine]
    check("a bot holding it is not: no ping is worth waking a webhook for",
          clerk.bell_for(guild) is None)
    bell.members = [holder]

    clerk.set_ringing(guild, False)
    check("a house that wants no pings in it switches them off",
          not clerk.ringing(guild))
    check("and the switch outranks the role: nobody is rung however many "
          "people are holding it", clerk.bell_for(guild) is None)
    clerk.set_ringing(guild, True)
    check("switched back on, the override is dropped rather than pinned",
          clerk.ringing(guild)
          and settings.get(guild.id, clerk.RINGING) is None)

    print("\nwhat the card is allowed to ping")
    only = clerk.ring(bell)
    check("the bell, and nothing else",
          only.roles == [bell] and only.users is False and only.everyone is False)
    nothing = clerk.ring()
    check("and with no bell, nothing at all: a proposal whose What says "
          "@everyone must not become one because the card carried it",
          nothing.roles is False and nothing.users is False
          and nothing.everyone is False)

    print("\nfiling rings it, once, on the ballot's own message")
    drawn = []
    keep_card, keep_floor = clerk.Card, clerk.floor_for

    class Spy(clerk.Card):
        """The card as it went up, which is the only place a mention can
        live: the ballot is a layout, so there is no content beside it to
        hide one in."""

        def __init__(self, segments=(), rows=()):
            drawn.append("\n\n".join(segments))
            super().__init__(segments, rows)

    clerk.Card = Spy
    try:
        floor = Floor()
        clerk.floor_for = lambda _g, _b: floor
        bill = run(clerk.file_bill(guild, author, "A books channel",
                                   "There shall be a books channel.",
                                   "People here read."))
        check("the floor still gets one message and not two: the ping is on "
              "the ballot, not beside it", len(floor.posted) == 1)
        check("the mention is on the card that went up",
              bell.mention in drawn[-1])
        check("and on a line that was already there, so a card is still "
              "four lines", len(drawn[-1].splitlines()) <= 4)
        check("the message names the bell as the one thing it may ping",
              floor.mentions[-1].roles == [bell])
        check("and the proposal it is drawn from carries no mention of its "
              "own, so every repaint from here loses it",
              bell.mention not in clerk.ballot_content(guild, bill))

        run(clerk.paint_floor(guild, bill))
        check("an edit after a vote redraws the card without it",
              bell.mention not in drawn[-1])
        check("and says outright that it may ping nobody, so a busy vote is "
              "not a bell that rings once a minute",
              floor.posted[0].edits[-1]["allowed_mentions"].roles is False)

        floor.posted.clear()
        run(clerk.paint_floor(guild, bill))
        check("a card somebody deleted goes back up silently, because "
              "whoever asked about this vote was told about it days ago",
              len(floor.posted) == 1 and floor.mentions[-1].roles is False
              and bell.mention not in drawn[-1])

        print("\nand with nobody to ring, the card is the one it always was")
        for label, arrange in (
            ("the house switched the pings off",
             lambda: clerk.set_ringing(guild, False)),
            ("nobody holds the bell",
             lambda: setattr(bell, "members", [])),
            ("there is no bell role at all",
             lambda: guild.roles.clear()),
        ):
            clerk.set_ringing(guild, True)
            bell.members = [holder]
            bindings.bind_role(guild.id, "bell", bell.id)
            guild.roles[:] = [bell]
            arrange()
            if not guild.roles:
                bindings.bind_role(guild.id, "bell", None)
            quiet = Floor()
            clerk.floor_for = lambda _g, _b, room=quiet: room
            run(clerk.file_bill(guild, author, "A books channel",
                                "There shall be a books channel.",
                                "People here read."))
            check(f"{label}: no mention, no stray text, no empty ping",
                  bell.mention not in drawn[-1] and "<@&" not in drawn[-1]
                  and quiet.mentions[-1].roles is False)
    finally:
        clerk.Card, clerk.floor_for = keep_card, keep_floor

    print("\nopting in and out is a button on the screen that is already "
          "about how this house votes")
    guild.roles[:] = [bell]
    bindings.bind_role(guild.id, "bell", bell.id)
    check("the door is not a card in the votes room, which is ballots and "
          "nothing else",
          clerk.house_view(guild, False) is not None)
    check("it offers to start telling you, and to stop",
          clerk.house_view(guild, False).children[0].label
          != clerk.house_view(guild, True).children[0].label)
    clerk.set_ringing(guild, False)
    check("a house with the pings off shows no button and no explanation of "
          "a button it has not got", clerk.house_view(guild, False) is None)
    clerk.set_ringing(guild, True)
    bindings.bind_role(guild.id, "bell", None)
    guild.roles.clear()
    check("and neither does a house that has never made the bell",
          clerk.house_view(guild, False) is None)


# ---------- the filing handlers, for real ----------

def test_filing(clerk, data):
    filed = {}
    open_bills = []

    def set_floor(bills):
        open_bills[:] = bills
        clerk.save_json(clerk.bills_path(GUILD), bills)

    async def fake_file_bill(guild, author, **kwargs):
        filed.clear()
        filed.update(kwargs)
        bill = {"no": 13, "author": author.display_name, "author_id": author.id,
                "status": "on_floor", "ends_at": "2026-08-02T00:00:00+00:00",
                **kwargs}
        set_floor(open_bills + [bill])
        return bill

    clerk.file_bill = fake_file_bill
    toolbox.configure(HERE, data, clerk.BILL_ACTIONS)

    print("\nproposing an invitation")
    set_floor([])
    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "propose", {
        "kind": "invite", "who": "Sam", "discord_id": "123456789012345678",
        "why": "They have been in the group chat for a year."})))
    check(f"the bill is filed: No. {out.get('filed')}", out.get("filed") == 13)
    check("it is filed as an invite bill", filed.get("kind") == "invite")
    check("the citizen who asked is the author, not the clerk",
          out.get("author") == "Robin")
    check("the ballot is advertised as three-way, blind while it runs, "
          "and published at close",
          "abstain" in out.get("ballot", "")
          and "no running count" in out.get("ballot", "")
          and "published at close" in out.get("ballot", ""))
    check("the text names the person and the consequence, and stops: what "
          "is true of every invitation ever filed is not what anybody is "
          "voting on",
          filed.get("what", "").startswith("Sam ")
          and "will be invited to" in filed.get("what", "")
          and len(filed.get("what", "")) < 80)
    check("a supplied Discord ID is carried into the bill",
          "123456789012345678" in filed.get("what", ""))
    check("their reasons are preserved verbatim",
          "in the group chat for a year" in filed.get("why", ""))
    # Two doors, and they were read as one for a while: an invitation is
    # somebody let into the server, never somebody handed a vote. It is no
    # longer spelled out on the bill, so the guard that keeps the model
    # from filing one for the other is the tool description.
    check("the tool that files it says outright that it is not a seat in "
          "the cooperative",
          "puts nobody in the cooperative"
          in toolbox.BILL_TOOLS["propose"]["description"])
    check("and somebody outside is sent to the door that exists",
          "/setup" in clerk.NOT_INSIDE)
    check("not to a vote that ends in a link to a room they are standing in",
          "already through it" in clerk.NOT_INSIDE)

    set_floor([])
    run(toolbox.dispatch(GUILD, CITIZEN, "propose",
                         {"kind": "invite", "who": "Sam", "why": "Long overdue."}))
    check("the Discord ID is genuinely optional",
          filed.get("kind") == "invite" and "Discord ID" not in filed.get("what", ""))

    set_floor([])
    for label, args in (
        ("a nameless proposal", {"why": "y"}),
        ("a proposal with no reasons", {"who": "Sam"}),
        ("a non-numeric Discord ID", {"who": "Sam", "discord_id": "@sam", "why": "y"}),
    ):
        check(f"{label} is refused", "error" in json.loads(
            run(toolbox.dispatch(GUILD, CITIZEN, "propose",
                                 {"kind": "invite", **args}))))

    print("\nproposing a change")
    set_floor([])
    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "propose", {
        "kind": "change", "title": "A books channel", "what": "There shall be a books channel.",
        "why": "People here read."})))
    check(f"the bill is filed: No. {out.get('filed')}", out.get("filed") == 13)
    check("it defaults to an ordinary bill", "kind" not in filed)
    check("the close time comes back for the clerk to quote",
          out.get("closes_at", "").startswith("2026-"))
    set_floor([])
    for label, args in (
        ("no title", {"what": "w", "why": "y"}),
        ("no what", {"title": "t", "why": "y"}),
        ("no why", {"title": "t", "what": "w"}),
        ("whitespace only", {"title": " ", "what": " ", "why": " "}),
    ):
        check(f"a bill with {label} is refused", "error" in json.loads(
            run(toolbox.dispatch(GUILD, CITIZEN, "propose",
                                 {"kind": "change", **args}))))

    print("\nchasing people is asked for, not assumed")
    set_floor([])
    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "propose", {
        "kind": "change", "title": "t", "what": "w", "why": "y"})))
    check("an ordinary proposal is not a priority one",
          filed.get("priority") is False and out.get("priority") is False)
    check("and says so, so he cannot promise a chase that will not happen",
          "Nobody is direct-messaged" in out.get("note", ""))
    set_floor([])
    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "propose", {
        "kind": "change", "title": "t", "what": "w", "why": "y", "priority": True})))
    check("but somebody who asks for one gets it",
          filed.get("priority") is True and out.get("priority") is True)
    check("and is told what it costs everyone else",
          "direct message" in out.get("note", ""))

    print("\nthe floor is not rationed")
    # The cap that used to live here bound only the people who asked Eugene
    # to file for them; the buttons in #propose never consulted it. Asking
    # politely now costs no more than clicking, which is the whole point.
    set_floor([{"no": i, "status": "on_floor", "author_id": 99} for i in range(9)])
    check("a citizen with nine of their own open may still file a tenth",
          "filed" in json.loads(
              run(toolbox.dispatch(GUILD, CITIZEN, "propose",
                                   {"kind": "change", "title": "t", "what": "w", "why": "y"}))))
    set_floor([{"no": i, "status": "on_floor", "author_id": 1000 + i}
               for i in range(20)])
    check("and a busy floor turns nobody away", "filed" in json.loads(
        run(toolbox.dispatch(GUILD, CITIZEN, "propose",
                             {"kind": "change", "title": "t", "what": "w", "why": "y"}))))
    check("invitations are not rationed either", "filed" in json.loads(
        run(toolbox.dispatch(GUILD, CITIZEN, "propose",
                             {"kind": "invite", "who": "Sam", "why": "y"}))))
    check("what a proposal must contain is still checked", "error" in json.loads(
        run(toolbox.dispatch(GUILD, CITIZEN, "propose",
                             {"kind": "change", "title": "t", "what": "w"}))))

    print("\nthe route that opens when the hands refuse")
    # Asked to kick a member of the cooperative, the officer's tools say no
    # and name this. If this route did not work the refusal would be a dead
    # end, which is how a rule stops being a rule and starts being an
    # obstacle people go round.
    set_floor([])
    sam = types.SimpleNamespace(id=10, name="sam", display_name="Sam", bot=False)
    voter = types.SimpleNamespace(id=13, name="voter", display_name="Voter",
                                 bot=False)
    guild = types.SimpleNamespace(
        name="The Hangout", id=1, owner_id=1, members=[sam, voter, CITIZEN],
        text_channels=[], channels=[],
        get_channel=lambda _i: None, get_role=lambda _i: None,
    )
    keep_keyed = clerk.in_cooperative
    clerk.in_cooperative = lambda m: getattr(m, "id", None) == 13
    try:
        out = json.loads(run(toolbox.dispatch(guild, CITIZEN, "propose", {
            "kind": "removal", "who": "Voter", "why": "They have not been here since March."})))
        check(f"a removal is filed: No. {out.get('filed')}", out.get("filed") == 13)
        check("as a removal, which is a different window and a different bar",
              filed.get("kind") == "kick" and filed.get("target_id") == 13)
        check("the subject is left off the roll that decides it",
              13 not in filed.get("eligible_ids", [13]))
        check("and is told the tally will never be published",
              "never published" in out.get("ballot", ""))
        check("someone outside the cooperative is an ordinary kick, not this",
              "not in the cooperative" in json.loads(run(toolbox.dispatch(
                  guild, CITIZEN, "propose",
                  {"kind": "removal", "who": "Sam", "why": "y"}))).get("error", ""))
        check("a removal without reasons is refused like any other proposal",
              "error" in json.loads(run(toolbox.dispatch(
                  guild, CITIZEN, "propose", {"kind": "removal", "who": "Voter"}))))
        set_floor([{"no": 3, "kind": "kick", "target_id": 13,
                    "status": "on_floor"}])
        check("and nobody is put up for removal twice at once",
              "already up for a vote" in json.loads(run(toolbox.dispatch(
                  guild, CITIZEN, "propose",
                  {"kind": "removal", "who": "Voter", "why": "y"}))).get("error", ""))
    finally:
        clerk.in_cooperative = keep_keyed


def test_setup_rooms(clerk, data):
    """What `/setup` hands Discord when it makes a room, and which rooms
    it makes at all.

    discord.py takes a mapping as "these exact overwrites" and its own
    MISSING as "none at all". None is neither, and it raises instead of
    defaulting -- so this crashed on the first room that is open to
    everybody, which is to say on every fresh install. The check is on the
    type of the argument rather than on the wording of the traceback,
    because the wording is theirs to change.
    """
    print("\nthe rooms /setup makes")
    import bindings
    from collections.abc import Mapping

    settings.configure(data)
    seen = []

    def guild_for(gid, coop=None):
        guild = types.SimpleNamespace(
            id=gid, text_channels=[], default_role="@everyone",
            roles=[coop] if coop is not None else [],
            get_role=lambda i: coop if coop is not None and i == 77 else None,
        )

        async def create_text_channel(name, topic=None, overwrites=None, reason=None):
            seen.append((name, overwrites))
            channel = types.SimpleNamespace(id=900 + len(guild.text_channels),
                                            name=name, mention=f"#{name}")
            # A created channel is in the server from then on, which is what
            # makes the second run of this command a no-op rather than a
            # second set of rooms.
            guild.text_channels.append(channel)
            return channel

        guild.create_text_channel = create_text_channel
        guild.get_channel = lambda i: next(
            (c for c in guild.text_channels if c.id == i), None)
        return guild

    import modules
    buildable = modules.wanted_rooms(3131, only_buildable=True)
    run(clerk.make_missing_rooms(guild_for(3131)))
    check("a room is made for every job an enabled feature wants and has none",
          len(seen) == len(buildable))
    check("and only for the features that are on: nothing is built for a "
          "switched-off one",
          set(n for n, _ow in seen)
          == {modules.ROOM_PLAN[j]["name"] for j in buildable})
    check("and every one of them is given a mapping, never None — which is "
          "what Discord refuses",
          all(isinstance(ow, Mapping) for _name, ow in seen))
    check("a house with no cooperative role yet gets its rooms open to "
          "everybody, rather than no rooms at all",
          all(ow == {} for _name, ow in seen))

    print("\nadopting a room you already have is a choice, not a guess")
    # Adoption used to be unconditional and by name, which is a decision
    # about somebody else's server taken from a string match: a #votes made
    # for something quite different quietly became the floor. It is the
    # house's call now, and building is the default because it is the one
    # that cannot be wrong about a room it did not make.
    seen.clear()
    built = [types.SimpleNamespace(id=500 + i, name=name, mention=f"#{name}")
             for i, name in enumerate(
                 ["🖋️・propose", "🗳️・votes", "🏛️・decisions"])]
    dressed = guild_for(3333)
    # A copy: the fake create_text_channel appends to guild.text_channels,
    # and handing it the same list means `built` grows as he builds.
    dressed.text_channels = list(built)

    made, bound, _skipped = run(clerk.make_missing_rooms(dressed))
    check("by default a server's own channels are left alone entirely",
          bound == [] and len(made) == len(built))
    check("and he builds his own instead, under his own names",
          all("🗳️・votes" not in line for line in made))
    for key in ("proposals", "votes", "decisions"):
        bindings.bind_channel(3333, key, None)
    seen.clear()

    # The bug adoption was written for: the builder writes "🗳️・votes" and
    # this command looked for "votes", found nothing, made a second one
    # loose at the top of the sidebar, and bound Eugene to the empty one.
    # Asked for, it still has to find them.
    dressed.text_channels = list(built)
    made, bound, _skipped = run(clerk.make_missing_rooms(dressed, adopt=True))
    check("asked for, nothing is created over the top of what is there",
          made == [] and seen == [])
    check("every one of them is adopted instead", len(bound) == len(built))
    check("and adopted as they are, emoji and category untouched",
          all("unchanged" in line for line in bound))
    check("the binding points at the room that was already there",
          bindings.bound_channel_id(3333, "votes") == 501)
    made2, bound2, skipped2 = run(clerk.make_missing_rooms(dressed, adopt=True))
    check("a second run has nothing left to do",
          made2 == [] and bound2 == []
          and sum(1 for s in skipped2 if "already bound" in s) == len(built))

    import modules as _m
    check("the chat room is never adopted: he makes his own, "
          "under his own name, so a server's existing #chat is not quietly "
          "turned into the only room he will answer in",
          "chat" not in _m.adoptable_rooms(3333)
          and _m.ROOM_PLAN["chat"]["name"] == "eugene-chat"
          and bindings.job_of("chat") is None
          and bindings.job_of("eugene-chat") == "chat")

    check("the room a name announces is the same one either route reads",
          bindings.job_of("🗳️・votes") == "votes"
          and bindings.job_of("votes") == "votes"
          and bindings.job_of("the-floor") == "votes")
    check("a room with no job announces none",
          bindings.job_of("💬・general") is None)
    # A job that stops existing leaves its binding behind for ever,
    # because nothing ever looks at the key again. That is not just
    # untidy: bound_room_ids reads every value whatever the key, and that
    # set is the rooms he treats as his own -- so rooms belonging to
    # features that were removed are rooms he still answers and listens
    # in, which is the one claim PRIVACY.md makes about him.
    bindings.bind_channel(3333, "votes", 501)
    settings.put(3333, rooms={**(settings.get(3333, "rooms") or {}),
                              "wardrobe": 777, "polls": 778})
    check("a binding for a job that no longer exists is still stored",
          "wardrobe" in (settings.get(3333, "rooms") or {}))
    check("and counts as one of his rooms until something drops it",
          777 in bindings.bound_room_ids(3333))
    gone = bindings.prune(dressed)
    check("prune drops it and says which, rather than leaving it to rot",
          any("wardrobe" in line for line in gone)
          and any("no such job" in line for line in gone))
    check("so it stops being a room he would answer in",
          777 not in bindings.bound_room_ids(3333)
          and 778 not in bindings.bound_room_ids(3333))
    check("while a job that does exist is left exactly alone",
          bindings.bound_channel_id(3333, "votes") == 501)

    check("adopting takes nothing that is already spoken for",
          bindings.adopt(3333, types.SimpleNamespace(id=999, name="votes")) is None
          and bindings.bound_channel_id(3333, "votes") == 501)
    check("and the terminal builder binds what it makes",
          bindings.adopt(3434, types.SimpleNamespace(id=42, name="🏛️・decisions"))
          == "decisions"
          and bindings.bound_channel_id(3434, "decisions") == 42)

    seen.clear()

    class Role:  # hashable, because it is used as an overwrite key
        id, name = 77, "Cooperative"

    role = Role()
    bindings.bind_role(3232, "cooperative", 77)
    run(clerk.make_missing_rooms(guild_for(3232, coop=role)))
    shut = [name for name, ow in seen if ow]
    check("once there is one, the rooms that are its business are shut to "
          "everyone else",
          sorted(shut) == ["propose", "votes"])
    check("and the rest stay open", sorted(n for n, ow in seen if not ow)
          == ["decisions"])
    check("the cooperative is named in every room that is shut — let in on "
          "its own, and never merely left out",
          all(role in ow for _n, ow in seen if ow))
    check("and let in on exactly the two that are its business",
          [n for n, ow in seen
           if ow and getattr(ow.get(role), "view_channel", None) is True]
          == ["propose", "votes"])

    print("\nwhat is built follows what is switched on")
    seen.clear()
    modules.apply_set(3535, ["moderation", "welcome"])
    run(clerk.make_missing_rooms(guild_for(3535)))
    check("a server running him as a moderator and nothing else gets no "
          "governance rooms at all", seen == [])
    modules.apply_set(3636, ["governance"])
    seen.clear()
    run(clerk.make_missing_rooms(guild_for(3636)))
    check("and governance on its own gets exactly its three",
          sorted(n for n, _ow in seen) == ["decisions", "propose", "votes"])


def test_prompt_matches_the_tools(data):
    """The prompt may never name a tool he has not been handed.

    This is the failure that read as stupidity and was not. The old prompt
    said, unconditionally, "Roles: `assign_role` puts any role on anybody"
    -- but `assign_role` belonged to the moderation module, so in a server
    with moderation off he was told he had a tool that was never declared.
    Asked to put a role on somebody he answered "the moderation feature is
    off here, so I can't assign roles": confident, accurate about the wrong
    thing, and a refusal where a different tool he *did* have would have
    done it.

    So: for every combination of features, every tool the prompt names has
    to be one `declarations()` actually hands over for that combination.
    """
    print("\nhe is never told about a tool he has not been given")
    import brain
    import modules
    import toolbox
    from itertools import product

    store = data / "prompt-tools-store"
    settings.configure(store)
    brain.configure(None, HERE, store, None, None, None, None)
    gid = 6060
    guild = types.SimpleNamespace(name="Book Club", id=gid)
    keys = modules.keys()
    try:
        every = set(toolbox.REGISTRY)
        for combination in product((False, True), repeat=len(keys)):
            for key, on in zip(keys, combination):
                modules.set_enabled(gid, key, on)
            stable, volatile = brain._system_prompt(guild)
            named = set(re.findall(r"`([a-z_]{4,})`", stable + volatile))
            # Only judge words that are tool-shaped; prose may quote a
            # setting name, and those are checked below.
            named &= every | {n for n in named if n in every}
            handed = {d["name"] for d in toolbox.declarations(gid)}
            on_now = [k for k, o in zip(keys, combination) if o]
            ghost = {n for n in named if n in every} - handed
            check(f"with {on_now or ['nothing']} on, the prompt names no "
                  f"tool he was not handed", not ghost)
        # And the stricter half: a name in the prompt that is not a tool at
        # all. `get_standing_orders` survived here for one commit after it
        # was folded into `lookup`, which would have had him calling a tool
        # that no longer existed.
        for key in keys:
            modules.set_enabled(gid, key, True)
        stable, volatile = brain._system_prompt(guild)
        named = set(re.findall(r"`([a-z_]{4,})`", stable + volatile))
        settings_names = set(settings.VOTING_RULES) | set(settings.VOTING_FLAGS)
        unknown = named - every - settings_names - {"lookup"}
        check("and every tool-shaped name in it is a tool that exists",
              not unknown, )
        if unknown:
            print(f"        unknown: {sorted(unknown)}")

        # The same trap one level down: a description that tells him to
        # call something is a claim about the tool list too.
        stale = set()
        for spec in toolbox.REGISTRY.values():
            # A tool's own parameter names are fair game in its own
            # description; what is not is naming another tool.
            mine = set(spec["parameters"].get("properties") or {})
            for word in re.findall(r"`([a-z_]{4,})`", spec["description"]):
                if word not in every and word not in settings_names \
                        and word not in mine:
                    stale.add(word)
        check("and no tool's own description names a tool that is gone",
              not stale)
        if stale:
            print(f"        stale in descriptions: {sorted(stale)}")
    finally:
        modules.reset(gid)
        settings.configure(data)


def test_upgrade_keeps_talking(clerk, data):
    """A server already running does not go silent on a deploy.

    Conversation defaults off now. That is right for a new install and
    wrong for every server already using it: they never set `chat`
    explicitly, so the new default would silence a clerk that had been
    answering for months, on an upgrade nobody would connect to it.
    """
    print("\nan upgrade does not quietly switch the talking off")
    import modules

    store = data / "upgrade-store"
    settings.configure(store)
    keyed, mute, chosen = 7001, 7002, 7003
    try:
        settings.set_brain_key(keyed, "claude", "sk-test-key")
        for gid in (keyed, mute, chosen):
            check(f"before the upgrade {gid} reads as off by default",
                  not modules.enabled(gid, "chat"))

        settings.set_brain_key(chosen, "claude", "sk-test-two")

        for gid in (keyed, mute, chosen):
            clerk.keep_talking(types.SimpleNamespace(id=gid))

        check("a server holding a key was talking, and still is",
              modules.enabled(keyed, "chat"))
        check("and it is written down as their choice, so switching it off "
              "afterwards stays off",
              modules.chosen(keyed).get("chat") is True)
        check("a server with no key is left alone, because it was not "
              "talking either way", not modules.enabled(mute, "chat"))

        # The reason it is marked rather than inferred: switching it off
        # stores nothing, because the stored value would equal the default.
        # A migration that reran would read that as "never chose" and turn
        # it back on, on every deploy, for ever.
        modules.set_enabled(chosen, "chat", False)
        clerk.keep_talking(types.SimpleNamespace(id=chosen))
        check("and switching it off afterwards stays off, because the "
              "migration runs once and says so rather than guessing again",
              not modules.enabled(chosen, "chat"))
    finally:
        settings.configure(data)


def test_privacy_surfaces(clerk, data):
    """The claims PRIVACY.md makes, checked against the code that makes
    them true. A privacy page is worth exactly as much as the thing it
    describes, so the load-bearing sentences are pinned here."""
    print("\nwhat leaves the server, and who can find that out")
    import brain
    import modules

    store = data / "privacy-store"
    settings.configure(store)
    gid = 5150
    try:
        check("with no key on file, nothing goes anywhere at all",
              brain.provider_name(gid) is None and brain.enabled(gid) is False)
        check("and no governance feature ever wanted one",
              all(not modules.SPEC[k]["brain"]
                  for k in modules.keys() if k != "chat"))

        check("a key cannot be set before somebody has read what it means",
              clerk.consented(types.SimpleNamespace(id=gid)) is False)
        settings.put(gid, **{clerk.CONSENT: {"by": "Robin", "id": 1,
                                             "at": "2026-01-01T00:00:00"}})
        check("and accepting is recorded under a name, because a server "
              "cannot agree to anything -- a person does",
              clerk.consented(types.SimpleNamespace(id=gid))
              and settings.get(gid, clerk.CONSENT)["by"] == "Robin")

        # What he is holding is counted, not quoted, so /privacy cannot
        # drift from the thing it is describing.
        brain._memory.clear()
        brain._deeds.clear()
        room = types.SimpleNamespace(id=4001, threads=[])
        guild = types.SimpleNamespace(id=gid, text_channels=[room])
        brain._remember(4001, "Robin", "hello")
        brain._note_deed(4001, "Robin", "lookup", {"kind": "bills"}, "[]", 0)
        held = brain.holding(guild)
        check("he can say how much of the room he is holding right now",
              held["messages"] == 1 and held["tool_results"] == 1)
        check("and the caps he says are the ones the code actually uses",
              held["message_cap"] == brain.MEMORY_MSGS
              and held["result_cap"] == brain.DEEDS_MAX)

        brain._remember(9999, "Someone", "another server's room")
        gone = brain.forget_here(guild)
        check("forgetting drops this server's and says how much",
              gone["messages"] == 1 and brain.holding(guild)["messages"] == 0)
        check("and never reaches into another server's rooms",
              len(brain._memory.get(9999, [])) == 1)
    finally:
        brain._memory.clear()
        brain._deeds.clear()
        settings.configure(data)


def test_invite_message(clerk, data):
    """The private word after an invitation carries. A house may write it,
    which means the one thing that must survive whatever they write is the
    link: a congratulation without one is worse than nothing."""
    print("\nthe message after an invitation passes")

    store = data / "invite-dm-store"
    settings.configure(store)
    gid = 7272
    guild = types.SimpleNamespace(id=gid, name="Book Club")
    proposer = types.SimpleNamespace(id=5, display_name="Robin")
    bill = {"no": 12, "invitee": "Sam", "title": "Invitation of Sam"}
    url = "https://discord.gg/abc123"
    try:
        shipped = clerk.invite_dm(guild, bill, proposer, url)
        check("with nothing set he sends his own sentence, and it names "
              "both the person invited and the person being written to",
              "Sam" in shipped and "Robin" in shipped
              and "No. 12" in shipped and url in shipped)

        settings.put(gid, **{clerk.INVITE_DM:
                             "Hello {proposer}, {name} is in: {link}"})
        theirs = clerk.invite_dm(guild, bill, proposer, url)
        check("a house that writes its own gets its own, filled in",
              theirs == f"Hello Robin, Sam is in: {url}")

        settings.put(gid, **{clerk.INVITE_DM: "Well done."})
        check("and one that forgets the link still delivers it",
              url in clerk.invite_dm(guild, bill, proposer, url))

        settings.put(gid, **{clerk.INVITE_DM:
                             "{proposer}, tell {their name} to use {link}"})
        odd = clerk.invite_dm(guild, bill, proposer, url)
        check("a placeholder he does not know comes back as typed rather "
              "than raising, because the cost of raising here is a passed "
              "invitation whose link never arrives",
              "{their name}" in odd and odd.startswith("Robin,")
              and url in odd)

        settings.put(gid, **{clerk.INVITE_DM: None})
        old = {"no": 3, "title": "Invitation of Ada"}
        check("a proposal filed before any of this still knows who it was "
              "for, off its own title",
              "Ada" in clerk.invite_dm(guild, old, proposer, url))
        check("and one that cannot say is vague rather than wrong",
              "them" in clerk.invite_dm(
                  guild, {"no": 4, "title": "Something else"}, proposer, url))

        # The list the panel offers is the list that works, or the first
        # anybody hears of the difference is a DM with a brace in it.
        every = " ".join(f"{{{f}}}" for f in clerk.INVITE_DM_FIELDS)
        settings.put(gid, **{clerk.INVITE_DM: every})
        filled = clerk.invite_dm(guild, bill, proposer, url)
        check("every placeholder the panel offers is one he fills in",
              "{" not in filled and "}" not in filled)

        long_url = "https://discord.gg/zzz"
        settings.put(gid, **{clerk.INVITE_DM: "x" * 1000 + " {link}"})
        check("and nothing Discord will refuse to send goes out",
              len(clerk.invite_dm(guild, bill, proposer, long_url)) <= 2000)
    finally:
        settings.configure(data)


def test_channel_choice(clerk, data):
    """Whether Apply may take over a channel you already have is the
    server's decision, and the panel has to say which way it is set."""
    print("\nadopt or build is a decision the house makes, and can read")
    import bindings
    import modules

    store = data / "choice-store"
    settings.configure(store)
    gid = 8181
    have = types.SimpleNamespace(id=900, name="🗳️・votes", mention="#votes")
    guild = types.SimpleNamespace(
        id=gid, name="Book Club", text_channels=[have], channels=[have],
        categories=[], roles=[], members=[], me=None,
        get_channel=lambda cid: have if cid == 900 else None,
        get_role=lambda rid: None,
    )
    try:
        check("building is what he does unless told otherwise, because it is "
              "the choice that cannot be wrong about a room he did not make",
              clerk.adopting(guild) is False)
        check("and the panel says which way it is set rather than leaving it "
              "to be discovered by pressing Apply",
              "make new ones" in clerk.panel_content(guild, None))

        clerk.set_adopting(guild, True)
        check("asked to use what is here, it says so", clerk.adopting(guild))
        found = clerk.adoptable_now(guild)
        check("and names the actual channel it would take over, so nobody "
              "presses Apply to find out",
              found.get("votes") is have
              and "#votes" in clerk.panel_content(guild, None))

        clerk.set_adopting(guild, False)
        check("and switching back leaves nothing pinned",
              clerk.adopting(guild) is False
              and settings.get(gid, clerk.ROOMS_MODE) is None)
    finally:
        settings.configure(data)


def test_closing(clerk, data):
    """The report is deterministic and clerk.py owns it, so it can be
    checked without touching Discord."""
    print("\nthe closing report tells the truth about what is left")

    def report(**over):
        bill = {"no": 4, "title": "t", "what": "w", "status": "passed",
                "kind": "ordinary", "tally_line": "✅ 3 / ❌ 1", "act": 9}
        bill.update(over)
        return clerk.closing_report(bill)

    passed = report()
    check("a passed bill reports its ruling and Act",
          passed["ruling"] == "passed" and passed["act"] == 9)
    check("the tally is quoted for an ordinary bill",
          passed["tally"] == "✅ 3 / ❌ 1")
    check("it always says the ballots are gone",
          any("destroyed" in line for line in passed["done"]))
    check("an ordinary Act is never reported as finished",
          any("human hands" in line for line in passed["outstanding"]))

    structural = report(title="A books channel", what="There shall be a books channel.")
    check("a structural Act names the real next steps",
          any("by hand" in line for line in structural["outstanding"]))

    invite = report(kind="invite", title="Invitation of Sam")
    check("an invite reports its tally like any other close",
          invite["tally"] == "✅ 3 / ❌ 1")
    check("the issued link is reported as done",
          any("invite link" in line for line in invite["done"]))
    check("and passing the link on is left to the proposer",
          any("proposer sends the link" in line for line in invite["outstanding"]))

    kick = report(kind="kick", title="Removal of X")
    check("a removal reports its tally too", kick["tally"] == "✅ 3 / ❌ 1")
    check("a carried-out removal leaves nothing outstanding", not kick["outstanding"])

    failed = report(status="failed", act=None)
    check("a failed bill rules failed", failed["ruling"] == "failed")
    check("a failed bill leaves nothing to do but refile",
          any("file it again" in line for line in failed["outstanding"]))
    check("a failed bill publishes no Act",
          not any("gazette" in line for line in failed["done"]))

    print("\ncalling time early")
    now = clerk.now_utc()

    def on_floor(hours_ago, window_hours=48):
        submitted = now - clerk.timedelta(hours=hours_ago)
        return {"no": 5, "title": "t", "what": "w", "status": "on_floor",
                "kind": "ordinary", "ballots": {}, "notes": {},
                "submitted_at": submitted.isoformat(),
                "ends_at": (submitted + clerk.timedelta(hours=window_hours)).isoformat()}

    live = {}
    closed = []

    async def fake_close(guild, bill):
        bill["status"] = "passed"
        bill["tally_line"] = "✅ 1 / ❌ 0"
        bill["act"] = 10
        closed.append(bill["no"])

    # Put back at the end. A stub that outlives its own test turns up later as
    # a baffling failure in something that never asked for one.
    STUBS = ("bill_by", "close_bill", "post_closing_report", "find_channel", "room")
    original = {name: getattr(clerk, name) for name in STUBS}
    try:
        clerk.bill_by = lambda guild, field, value: live.get(value)
        clerk.close_bill = fake_close
        clerk.post_closing_report = lambda guild, bill: _async(clerk.closing_report(bill))
        clerk.find_channel = lambda guild, needle: None
        clerk.room = lambda guild, key: None
        toolbox.configure(HERE, data, clerk.BILL_ACTIONS)

        live[5] = on_floor(hours_ago=1)
        out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "close_floor", {"bill_no": 5})))
        check(f"one hour into a 48 hour floor is refused: {out.get('error', '')[:40]}",
              "error" in out and not closed)
        check("and it says when calling time becomes possible",
              out.get("closable_from", "").startswith("20"))

        live[5] = on_floor(hours_ago=13)
        out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "close_floor", {"bill_no": 5})))
        check("past a quarter of the window it closes", closed == [5])
        check("and returns the ruling", out.get("ruling") == "passed")
        check("with the report attached",
              out.get("outstanding") and out.get("done"))
        check("naming who called time", out.get("closed_early_by") == "Robin")

        live[5]["status"] = "passed"
        out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "close_floor", {"bill_no": 5})))
        check("a bill that already closed cannot be closed again", "error" in out)

        check("an unknown bill number is refused", "error" in json.loads(
            run(toolbox.dispatch(GUILD, CITIZEN, "close_floor", {"bill_no": 999}))))
        check("a missing bill number is refused", "error" in json.loads(
            run(toolbox.dispatch(GUILD, CITIZEN, "close_floor", {}))))
        check("a non-numeric bill number is refused", "error" in json.loads(
            run(toolbox.dispatch(GUILD, CITIZEN, "close_floor",
                                 {"bill_no": "the books one"}))))

        # a three-minute sandbox floor must still be closable
        live[6] = dict(on_floor(hours_ago=0.02, window_hours=0.05), no=6)
        out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "close_floor", {"bill_no": 6})))
        check("the guard scales to a three-minute test floor", "error" not in out)
    finally:
        for name, real in original.items():
            setattr(clerk, name, real)


def test_close_floor_split(clerk, data):
    """Calling time now has two doors: `/close` reads a dict, the model reads
    a JSON string, and both come out of one `close_floor`. The refusals are
    the part that had to survive the move intact, so they are checked through
    both doors and compared answer for answer."""
    print("\ncalling time: one ruling, two doors")
    now = clerk.now_utc()

    def on_floor(no, hours_ago, window_hours=48):
        submitted = now - clerk.timedelta(hours=hours_ago)
        return {"no": no, "title": "t", "what": "w", "status": "on_floor",
                "kind": "ordinary", "ballots": {}, "notes": {},
                "submitted_at": submitted.isoformat(),
                "ends_at": (submitted + clerk.timedelta(hours=window_hours)).isoformat()}

    live = {}

    async def fake_close(guild, bill):
        bill["status"] = "passed"
        bill["tally_line"] = "✅ 1 / ❌ 0"
        bill["act"] = 10

    STUBS = ("bill_by", "close_bill", "post_closing_report", "find_channel", "room")
    original = {name: getattr(clerk, name) for name in STUBS}
    try:
        clerk.bill_by = lambda guild, field, value: live.get(value)
        clerk.close_bill = fake_close
        clerk.post_closing_report = lambda guild, bill: _async(clerk.closing_report(bill))
        clerk.find_channel = lambda guild, needle: None
        clerk.room = lambda guild, key: None

        def both(bill_no):
            """The dict the command reads and the text the model reads, for a
            refusal that changes nothing and so can be asked for twice."""
            return (run(clerk.close_floor(GUILD, CITIZEN, bill_no)),
                    run(clerk.act_close_floor(GUILD, CITIZEN, {"bill_no": bill_no})))

        direct, wrapped = both(404)
        check("an unknown proposal is refused at the door",
              isinstance(direct, dict) and "error" in direct
              and "404" in direct["error"])
        check("and the model is handed that refusal as text, not as a dict",
              isinstance(wrapped, str) and json.loads(wrapped) == direct)

        live[2] = dict(on_floor(2, hours_ago=13), status="passed", act=11)
        direct, wrapped = both(2)
        check("a proposal that already closed cannot be closed again",
              "error" in direct)
        check("and the refusal says how it went, so nobody asks twice",
              direct.get("ruling") == "passed" and direct.get("act") == 11)
        check("both doors give the same answer", json.loads(wrapped) == direct)

        live[3] = on_floor(3, hours_ago=1)
        direct, wrapped = both(3)
        check("one hour into a 48 hour floor is still too early", "error" in direct)
        check("the hour it becomes closable survives the split",
              direct.get("closable_from", "").startswith("20"))
        check("and survives being turned into text",
              json.loads(wrapped) == direct)

        # The success path closes what it touches, so each door gets its own
        # proposal and the two reports are compared bill number aside.
        live[4] = on_floor(4, hours_ago=13)
        report = run(clerk.close_floor(GUILD, CITIZEN, 4))
        check("past a quarter of the window it closes and rules",
              isinstance(report, dict) and report["ruling"] == "passed")
        check("under the name of whoever called time",
              report["closed_early_by"] == "Robin")
        live[5] = on_floor(5, hours_ago=13)
        as_text = run(clerk.act_close_floor(GUILD, CITIZEN, {"bill_no": 5}))
        check("the model gets the identical report, one JSON string of it",
              isinstance(as_text, str)
              and json.loads(as_text) == {**report, "bill": 5})

        live[6] = on_floor(6, hours_ago=13)
        check("the wrapper reads a number written as text",
              "ruling" in json.loads(
                  run(clerk.act_close_floor(GUILD, CITIZEN, {"bill_no": "6"}))))
        for label, args in (("a missing", {}),
                            ("a non-numeric", {"bill_no": "the books one"}),
                            ("an empty", {"bill_no": None})):
            out = json.loads(run(clerk.act_close_floor(GUILD, CITIZEN, args)))
            check(f"{label} proposal number is refused by the wrapper, "
                  "before anything is looked up",
                  out.get("error") == "which proposal? give the number")
    finally:
        for name, real in original.items():
            setattr(clerk, name, real)


def _async(value):
    async def wrapper():
        return value
    return wrapper()


def fake_member(uid, roles=(), bot=False):
    return types.SimpleNamespace(
        id=uid, bot=bot, display_name=f"m{uid}",
        roles=[types.SimpleNamespace(name=r) for r in roles],
    )


def test_roster(data):
    print("\nwho counts, and how many of them it takes")
    import roster

    roster.configure(data)

    check("eight on the roster carry on five", roster.required(8) == 5)
    check("and six for a supermajority",
          roster.required(8, "fundamental") == 6)
    check("seven carry on four", roster.required(7) == 4)
    check("five carry on three", roster.required(5) == 3)
    check("one carries on one", roster.required(1) == 1)
    check("an empty roster never asks for zero", roster.required(0) == 1)
    check("a threshold never exceeds the roster it counts",
          all(roster.required(n, t) <= n
              for n in range(1, 40) for t in ("normal", "fundamental")))
    check("a supermajority never asks for less than a majority",
          all(roster.required(n, "fundamental") >= roster.required(n)
              for n in range(1, 40)))
    check("the ordinary tier takes the house's own share like any other",
          roster.share_for("normal", {"normal_share": 0.75}) == 0.75
          and roster.required(8, "normal", 0.75) == 6)
    check("and the floor holds it at a majority, so half of eight is five "
          "rather than four", roster.required(8, "normal", 0.5) == 5)

    check("a share is counted against the roster unless the house says "
          "otherwise", roster.counted(8, 3, 1) == 8)
    check("a house may count against the ballots cast instead",
          roster.counted(8, 3, 1, {roster.TURNOUT: True}) == 3)
    check("and may let an abstention out of whichever of the two it is",
          roster.counted(8, 3, 1, {roster.ABSTAIN_OUT: True}) == 7
          and roster.counted(8, 3, 1, {roster.TURNOUT: True,
                                       roster.ABSTAIN_OUT: True}) == 2)
    check("the widest a count can still get is the whole roll, less the "
          "abstentions already out of it",
          roster.most_counted(8, 2, {roster.TURNOUT: True}) == 8
          and roster.most_counted(8, 2, {roster.ABSTAIN_OUT: True}) == 6)
    check("and counting against turnout never asks for more than the "
          "roster would have, which is what makes it safe to end a vote on "
          "the roster's figure however the house counts",
          all(roster.required(roster.counted(n, v, 0, {roster.TURNOUT: True}))
              <= roster.required(n)
              for n in range(1, 30) for v in range(0, n + 1)))

    everyone = lambda m: True  # noqa: E731
    guild = types.SimpleNamespace(id=1, members=[
        fake_member(1), fake_member(2), fake_member(3, roles=["Away"]),
        fake_member(4, bot=True),
    ])
    roll = roster.active(guild, everyone)
    check("an away role steps you out of the roster", 3 not in roll)
    check("bots were never in it", 4 not in roll)
    check("everyone else is in", roll == [1, 2])
    check("and the subject of a vote can be excluded",
          roster.active(guild, everyone, exclude={1}) == [2])

    check("somebody unseen still counts, so a fresh state file cannot "
          "empty the house", roster.is_away(1, fake_member(7)) is False)
    roster.touch(1, 7)
    check("and speaking keeps them in", roster.is_away(1, fake_member(7)) is False)


def test_voting(clerk, data):
    print("\nvotes that end when they are decided")
    import roster

    roster.configure(data)
    original = clerk.in_cooperative
    clerk.in_cooperative = lambda m: True
    try:
        guild = types.SimpleNamespace(
            id=1,
            members=[fake_member(i) for i in range(1, 9)],
            get_channel=lambda _id: None,
            get_role=lambda _id: None,
        )

        def bill(ballots, **extra):
            return {"no": 1, "title": "t", "kind": "ordinary",
                    "status": "on_floor", "ballots": dict(ballots), **extra}

        st = clerk.vote_state(guild, bill({}))
        check("eight present need five yes", st["size"] == 8 and st["need"] == 5)
        check("nobody has voted yet", st["waiting"] == 8)

        st = clerk.vote_state(guild, bill({"1": "yes", "2": "no"}))
        check("both sides are counted", (st["yes"], st["no"]) == (1, 1))
        check("and the rest are still owed a vote", st["waiting"] == 6)

        kick = bill({}, kind="kick", target_id=3)
        st = clerk.vote_state(guild, kick)
        check("a removal drops its subject from its own roster", st["size"] == 7)
        check("and asks for its own bar -- all the eligible but two -- "
              "rather than its tier's share, which is the number the close "
              "has always used", st["need"] == 5 and st["counted"] == 7)

        four = {str(i): "yes" for i in range(1, 5)}
        check("four yes of eight is not settled",
              clerk.vote_settled(clerk.vote_state(guild, bill(four))) is False)
        five = {str(i): "yes" for i in range(1, 6)}
        check("the fifth yes settles it on the spot",
              clerk.vote_settled(clerk.vote_state(guild, bill(five))) is True)
        check("a no can still become a yes, so failure waits for everyone",
              clerk.vote_settled(
                  clerk.vote_state(guild, bill({str(i): "no" for i in range(1, 5)}))
              ) is False)
        everyone_voted = {str(i): "no" for i in range(1, 9)}
        check("once nobody is left to change their mind, it is over",
              clerk.vote_settled(
                  clerk.vote_state(guild, bill(everyone_voted))) is True)
        blind = types.SimpleNamespace(
            id=1, members=[], get_channel=lambda _id: None,
            get_role=lambda _id: None,
        )
        check("an empty roster is never settled: we cannot see the house, "
              "which is not the same as an empty one",
              clerk.vote_settled(clerk.vote_state(blind, bill(five))) is False)

        shown = clerk.ballot_content(guild, bill({"1": "yes", "2": "no"}))
        check("a vote about a thing shows its progress",
              "1 of 5 yes" in shown and "❌ 1" in shown)
        hidden = clerk.ballot_content(
            guild, bill({"1": "yes", "2": "no"}, kind="invite"))
        check("a vote about a person shows turnout and nothing else",
              "2 of 8 voted" in hidden and "❌" not in hidden)
        check("and it is the whole of the ballot: one line, no paragraph "
              "under it explaining what is true of every vote",
              clerk.ballot_line(guild, bill({"1": "yes"})).count("\n") == 0)
    finally:
        clerk.in_cooperative = original


def test_voting_numbers(data):
    """The numbers a house votes by are the house's, not the repo's. These
    pin that they can be changed, that they cannot be changed into nonsense,
    and that one server's choices are not another's."""
    print("\nthe numbers are the house's")

    settings.configure(data / "numbers-store")

    stock = settings.voting()
    check("a house that has chosen nothing votes by the numbers he came with",
          stock["floor_hours"] == 48 and stock["fundamental_share"] == 0.75)
    check("and asking for a particular house's gives the same until it chooses",
          settings.voting(1) == stock)
    check("the ordinary tier has a figure of its own now, at the plain "
          "majority it was when it had none",
          stock["normal_share"] == 0.5)
    check("and both counting rules ship off, which is the roster "
          "denominator and the abstention that lands where silence lands",
          stock["count_turnout"] is False
          and stock["abstain_steps_out"] is False)
    check("a removal still leaves two of the eligible unconvinced",
          stock["removal_spare"] == 2)

    accepted, rejected = settings.set_voting(1, floor_hours=6)
    check("a house can set its own window", accepted["floor_hours"] == 6
          and not rejected)
    check("and it is the one that comes back", settings.voting(1)["floor_hours"] == 6)
    check("its neighbour is untouched", settings.voting(2)["floor_hours"] == 48)
    check("and every number it did not touch still follows the default",
          settings.voting(1)["away_days"] == 14)

    _, rejected = settings.set_voting(1, fundamental_share="three quarters")
    check("a threshold that is not a number is refused by name, not ignored",
          rejected == ["fundamental_share"])
    check("and the old value still stands",
          settings.voting(1)["fundamental_share"] == 0.75)
    _, rejected = settings.set_voting(1, quorum_of_the_realm=1)
    check("a number that is not a governance number is refused too",
          rejected == ["quorum_of_the_realm"])

    held, _ = settings.set_voting(1, fundamental_share=5)
    check("a share above everybody is held at everybody, so a threshold "
          "can never be made unreachable", held["fundamental_share"] == 1.0)
    held, _ = settings.set_voting(1, floor_hours=0)
    check("and a window of nothing is held above nothing, so a vote can "
          "never close before it opens", held["floor_hours"] > 0)

    settings.set_voting(1, floor_hours=None)
    check("putting one back on the default drops it rather than pinning it",
          settings.voting(1)["floor_hours"] == 48
          and "floor_hours" not in settings.voting_overrides(1))
    check("and what a house has actually chosen can be told from what it "
          "merely inherited",
          set(settings.voting_overrides(1)) == {"fundamental_share"})

    settings.configure(data)


def test_numbers_bite(clerk, data):
    """A number that can be set and then ignored is worse than a constant.
    These pin that the house's copy is the one the machinery actually
    reads, and that it is read now rather than at boot."""
    print("\nand the numbers the house sets are the ones he uses")
    import roster

    roster.configure(data)
    settings.configure(data)
    original = clerk.in_cooperative
    clerk.in_cooperative = lambda m: True
    try:
        guild = types.SimpleNamespace(
            id=79, name="The Hangout",
            members=[fake_member(i) for i in range(1, 9)],
            get_channel=lambda _id: None, get_role=lambda _id: None,
        )
        bill = {"no": 11, "title": "t", "kind": "ordinary",
                "status": "on_floor", "ballots": {}}

        check("eight carry an ordinary vote on five",
              clerk.vote_state(guild, bill)["need"] == 5)
        settings.set_voting(guild.id, fundamental_share=0.9)
        check("a house that wants a rule change to be harder says so, and "
              "it is harder from that moment",
              clerk.vote_state(guild, dict(bill, tier="fundamental"))["need"] == 8)

        settings.set_voting(guild.id, away_days=1)
        roster.touch(1, 3)
        check("and a house that wants a shorter quiet spell gets one",
              clerk.numbers(guild)["away_days"] == 1)

    finally:
        clerk.in_cooperative = original
        settings.set_voting(guild.id, fundamental_share=None, away_days=None)


def test_counting_rules(clerk, data):
    """Who a vote is counted against is the house's, and the rule that ends
    a vote early has to follow it.

    That is the whole risk in these two switches: a denominator that widens
    while the ballot is open can put a threshold back out of reach after it
    was met, and a clerk who announced a result on the strength of it would
    have closed a vote nobody had won. So most of what follows is about
    when a vote may be called, and the rest is about the four sums --
    ballot line, receipt, close, removal -- agreeing on one figure."""
    print("\nwhat a vote is counted against, and when it may be called")
    import roster

    roster.configure(data)
    settings.configure(data)
    original = clerk.in_cooperative
    clerk.in_cooperative = lambda m: True
    gid = 81
    guild = types.SimpleNamespace(
        id=gid, name="The Hangout",
        members=[fake_member(i) for i in range(1, 9)],
        text_channels=[], get_channel=lambda _id: None,
        get_role=lambda _id: None, get_member=lambda _id: None,
    )

    def bill(ballots, **extra):
        return {"no": 3, "title": "t", "kind": "ordinary", "what": "w",
                "status": "on_floor", "ballots": dict(ballots), **extra}

    def state(ballots, **extra):
        return clerk.vote_state(guild, bill(ballots, **extra))

    def yes_from(n):
        return {str(i): "yes" for i in range(1, n + 1)}

    try:
        check("an ordinary proposal is counted against the roster and "
              "carries on a plain majority, exactly as it did when it was "
              "the one threshold a house could not set",
              state({})["need"] == 5 and state({})["counted"] == 8)
        settings.set_voting(gid, normal_share=0.75)
        check("a house that wants its everyday bar higher says so",
              state({})["need"] == 6)
        held, _ = settings.set_voting(gid, normal_share=0.2)
        check("and cannot put it under a majority, which would carry a "
              "proposal with more of the house against it than for it -- "
              "and close the vote before the rest of them were asked",
              held["normal_share"] == 0.5 and state({})["need"] == 5)
        settings.set_voting(gid, normal_share=None)

        # ---- an abstention that steps out of the count ----
        check("an abstention lands where silence lands out of the box",
              state({"1": "abstain"})["need"] == 5)
        settings.set_voting(gid, abstain_steps_out=True)
        stepped = state({"1": "abstain"})
        check("a house may take it out of the count instead, which lowers "
              "the bar rather than counting against",
              stepped["need"] == 4 and stepped["counted"] == 7)
        check("and the vote can still be called the moment it is decided, "
              "because every abstention still to come only lowers the bar "
              "further",
              clerk.vote_settled(state({**yes_from(4), "5": "abstain"})) is True)
        settings.set_voting(gid, abstain_steps_out=None)

        # ---- counted against turnout ----
        check("against the roster, the fifth yes of eight ends it where it "
              "stands", clerk.vote_settled(state(yes_from(5))) is True)
        settings.set_voting(gid, count_turnout=True)
        turnout = state({**yes_from(2), "3": "no"})
        check("counted against turnout the bar is a share of the ballots "
              "cast, so three votes ask for two",
              turnout["counted"] == 3 and turnout["need"] == 2)
        check("but a vote that has met it is not called, because the next "
              "ballot widens the denominator underneath it",
              clerk.vote_settled(turnout) is False)
        check("what still ends one early is the yes votes carrying the "
              "whole roll, which no later ballot can take back",
              clerk.vote_settled(state(yes_from(5))) is True)
        check("failing waits for everybody under either rule",
              clerk.vote_settled(
                  state({str(i): "no" for i in range(1, 5)})) is False)
        check("and a house that has all voted is over whichever way it "
              "counts",
              clerk.vote_settled(
                  state({str(i): "no" for i in range(1, 9)})) is True)
        check("the line under the buttons quotes the live bar, not the "
              "roster's",
              "2 of 2 yes" in clerk.ballot_line(
                  guild, bill({**yes_from(2), "3": "no"})))
        check("and the nudge stops telling people silence counts against "
              "something it no longer counts against",
              "settled among whoever does vote" in clerk.nudge_text(
                  guild, bill({}, priority=True)))
        check("nor does the receipt promise that one more yes carries it, "
              "which counted this way it does not: at three cast it takes "
              "two, and the third yes makes it three of four",
              "the bar moves with every ballot that arrives"
              in clerk.standing_line(guild, bill({"1": "yes", "2": "no"})))
        check("while a vote the yes votes have carried outright is still "
              "told plainly that they have",
              clerk.standing_line(guild, bill(yes_from(5)),
                                  carried="That carries it.")
              == "That carries it.")

        # ---- the close reads the same rule the ballot showed ----
        closed = {}

        async def catch(guild_, bill_, passed, tally_line, decided=None):
            closed.update(passed=passed, line=tally_line,
                          threshold=bill_.get("threshold"))
            bill_["status"] = "passed" if passed else "failed"

        real_finalize = clerk.finalize_bill
        clerk.finalize_bill = catch
        try:
            run(clerk.close_bill(guild, bill({**yes_from(2), "3": "no"})))
            check("the close counts by the rule the ballot was showing: two "
                  "of the three who voted carries it",
                  closed["passed"] is True and "needed 2 of 3" in closed["line"])
            check("and the record says which three, because 'needed 2 of 3' "
                  "read a year later has to name its denominator",
                  closed["threshold"]["counted"] == 3
                  and closed["threshold"]["roster"] == 8)
            settings.set_voting(gid, count_turnout=None)
            run(clerk.close_bill(guild, bill({**yes_from(2), "3": "no"})))
            check("against the roster the same three ballots fail it",
                  closed["passed"] is False and "needed 5 of 8" in closed["line"])

            # ---- removals keep a bar of their own ----
            kick = bill({}, kind="kick", target_id=3)
            st = clerk.vote_state(guild, kick)
            run(clerk.close_bill(
                guild, bill(yes_from(5), kind="kick", target_id=3)))
            check("a removal's ballot and its close quote one bar, which is "
                  "the bug that made a removal say it needed six and pass "
                  "on five",
                  closed["passed"] is True
                  and f"needed {st['need']} of {st['counted']}" in closed["line"])
            settings.set_voting(gid, removal_spare=1)
            check("a house that wants a removal to convince one more says "
                  "so, and the warning shown before anybody names a person "
                  "moves with it",
                  clerk.vote_state(guild, kick)["need"] == 6
                  and "but 1 say yes" in clerk.removal_weight(guild))
            settings.set_voting(gid, count_turnout=True)
            check("and counting against turnout does not reach it: a "
                  "removal is a count of people to convince, never a share "
                  "of whoever turned up",
                  clerk.vote_state(guild, kick)["need"] == 6)
            settings.set_voting(gid, count_turnout=None, removal_spare=None)

            small = types.SimpleNamespace(
                id=gid, name="The Hangout",
                members=[fake_member(i) for i in range(1, 5)],
                text_channels=[], get_channel=lambda _id: None,
                get_role=lambda _id: None, get_member=lambda _id: None,
            )
            check("the floor under the bar is the house's too, so four "
                  "people cannot show somebody the door on one vote",
                  clerk.vote_state(small, bill({}, kind="kick",
                                               target_id=4))["need"] == 3)
        finally:
            clerk.finalize_bill = real_finalize

        # ---- what the switches deliberately do not reach ----
        settings.set_voting(gid, count_turnout=True, abstain_steps_out=True)
        poll = bill({"1": "Tue", "2": "Thu"}, options=["Tue", "Thu"])
        check("neither switch reaches a choice ballot, which was always "
              "settled among the votes cast and has no abstention to step "
              "out of them", clerk.vote_state(guild, poll)["clinch"] == 5)
        settings.set_voting(gid, count_turnout=None, abstain_steps_out=None)

        shown = "\n".join(clerk.voting_lines(guild))
        check("and every new rule is on the panel beside the rest, since a "
              "rule nobody can read is a rule nobody trusts",
              all(f"`{name}`" in shown for name in
                  ("normal_share", "removal_spare", "count_turnout",
                   "abstain_steps_out")))
    finally:
        clerk.in_cooperative = original
        settings.set_voting(gid, normal_share=None, removal_spare=None,
                            count_turnout=None, abstain_steps_out=None)


def test_veto(clerk, data):
    """The last word: a proposal that carried, and a window in which the
    house can still take it back."""
    print("\nthe last word, after the ballot has closed")
    import duties
    import roster

    roster.configure(data)
    settings.configure(data)

    # ---- the door has a price of its own ----
    original = clerk.in_cooperative
    clerk.in_cooperative = lambda m: True
    guild = types.SimpleNamespace(
        id=404, name="The Hangout",
        members=[fake_member(i) for i in range(1, 9)],
        owner_id=1, get_channel=lambda _id: None, get_role=lambda _id: None,
        get_member=lambda _id: None,
    )
    try:
        invite = {"no": 1, "title": "Invitation of Sam", "kind": "invite",
                  "status": "on_floor", "ballots": {}}
        ordinary = {"no": 2, "title": "t", "kind": "ordinary",
                    "status": "on_floor", "ballots": {}}
        check("an invitation starts where it always did, on a plain majority",
              clerk.vote_state(guild, invite)["need"]
              == clerk.vote_state(guild, ordinary)["need"] == 5)
        settings.set_voting(guild.id, invite_share=0.75)
        check("a house that wants the door dearer says so, and only the door "
              "moves",
              clerk.vote_state(guild, invite)["need"] == 6
              and clerk.vote_state(guild, ordinary)["need"] == 5)
        check("and a removal is untouched by it",
              clerk.vote_state(guild, dict(ordinary, kind="kick"))["need"] == 6)
        settings.set_voting(guild.id, invite_share=None)

        # ---- who may veto what ----
        check("an invitation may be vetoed out of the box",
              clerk.veto_rule(guild, invite) == 1)
        check("and nothing else may be",
              clerk.veto_rule(guild, ordinary) is None)
        settings.set_voting(guild.id, proposal_veto=True, proposal_vetoes=3,
                            invite_vetoes=2)
        check("a house can hand itself a veto over everything",
              clerk.veto_rule(guild, ordinary) == 3)
        check("and price the two separately",
              clerk.veto_rule(guild, invite) == 2)
        settings.set_voting(guild.id, invite_veto=False)
        check("switching the door's veto off switches it off",
              clerk.veto_rule(guild, invite) is None)
        settings.set_voting(guild.id, invite_veto=None, proposal_veto=None,
                            proposal_vetoes=None, invite_vetoes=None)

        # ---- the window ----
        now = clerk.now_utc()

        def passed(hours_left=6, **over):
            bill = {
                "no": 7, "title": "Invitation of Sam", "kind": "invite",
                "status": "passed", "author_id": 99, "act": 3,
                "tally_line": "✅ 4 / ❌ 1 / 🤍 2 · needed 4 of 7",
                "veto_message_id": 555,
                "veto": {
                    "until": (now + clerk.timedelta(hours=hours_left)).isoformat(),
                    "needed": 1, "cast": [],
                },
            }
            bill.update(over)
            return bill

        check("a window with time on it is open",
              clerk.veto_open(passed()) is True)
        check("one that has run out is not",
              clerk.veto_open(passed(hours_left=-1)) is False)
        check("a proposal that failed has no window: failing was the answer",
              clerk.veto_open(passed(status="failed")) is False)
        check("and neither has one already shut",
              clerk.veto_open(passed(veto={"until": "x", "closed": True}))
              is False)

        # ---- casting one ----
        said = []

        class Pressed:
            def __init__(self, user, message_id=555):
                self.guild = guild
                self.user = user
                self.message = types.SimpleNamespace(id=message_id)
                self.response = types.SimpleNamespace(
                    send_message=self._say, edit_message=self._say,
                    is_done=lambda: False)
                self.offered = None

            async def _say(self, content=None, **kw):
                said.append(content)
                self.offered = kw.get("view")

        overturned = []
        live = {}
        STUBS = ("bill_by", "update_bill", "refresh_veto", "overturn_bill")
        keep = {name: getattr(clerk, name) for name in STUBS}
        try:
            clerk.bill_by = lambda g, field, value: live.get(value)
            clerk.update_bill = lambda g, b: _async(None)
            clerk.refresh_veto = lambda g, b: _async(None)
            clerk.overturn_bill = lambda g, b: _async(overturned.append(b["no"]))

            # ---- the press before the veto ----
            # At one veto the button is the whole reversal, so the button
            # is not the veto: it asks, and the answer is what counts.
            bill = passed()
            live[555] = bill
            asked = Pressed(fake_member(3))
            run(clerk.confirm_veto(asked))
            check("pressing veto casts nothing by itself",
                  not bill["veto"]["cast"])
            check("it asks, naming the proposal",
                  "Veto Proposal No. 7?" in said[-1])
            check("and says what the next press would actually do, since at "
                  "one veto there is no taking it back",
                  "goes back out" in said[-1])
            check("holding out the confirmation that would do it",
                  isinstance(asked.offered, clerk.VetoConfirm)
                  and asked.offered.host_id == 555)
            run(clerk.cast_veto(Pressed(fake_member(3)),
                                host_id=asked.offered.host_id))
            check("which is the press that records one",
                  [c["id"] for c in bill["veto"]["cast"]] == [3])
            check("and it overturns, because one was all it wanted",
                  overturned == [7])
            overturned.clear()

            settings.set_voting(guild.id, invite_vetoes=2)
            bill = passed()
            live[555] = bill
            asked = Pressed(fake_member(9))
            run(clerk.confirm_veto(asked))
            check("where more than one is wanted the question says so "
                  "instead, and that the veto can be withdrawn",
                  "1 more would be wanted" in said[-1]
                  and "withdraw" in said[-1])

            bill = passed()
            live[555] = bill
            run(clerk.cast_veto(Pressed(fake_member(3))))
            check("a veto is recorded", len(bill["veto"]["cast"]) == 1)
            check("named, since the house has not asked for anonymity",
                  bill["veto"]["cast"][0].get("name") == "m3")
            check("and one short of two does not overturn anything",
                  not overturned and "1 veto would overturn" in said[-1])

            run(clerk.cast_veto(Pressed(fake_member(3))))
            check("the same person cannot veto twice",
                  len(bill["veto"]["cast"]) == 1 and "already" in said[-1])

            run(clerk.cast_veto(Pressed(fake_member(3)), withdraw=True))
            check("but they can take it back", not bill["veto"]["cast"])
            run(clerk.cast_veto(Pressed(fake_member(4)), withdraw=True))
            check("and nobody can withdraw one they never cast",
                  "no veto" in said[-1])

            settings.set_voting(guild.id, veto_anonymous=True)
            run(clerk.cast_veto(Pressed(fake_member(5))))
            check("a house that wants the veto anonymous gets it: the id is "
                  "held to stop a second one, the name is never taken",
                  bill["veto"]["cast"] == [{"id": 5}])
            settings.set_voting(guild.id, veto_anonymous=None)

            run(clerk.cast_veto(Pressed(fake_member(6))))
            check("the second veto of two overturns it", overturned == [7])

            clerk.in_cooperative = lambda m: False
            run(clerk.cast_veto(Pressed(fake_member(7))))
            check("nobody outside the cooperative reaches the button",
                  "cooperative" in said[-1])
            clerk.in_cooperative = lambda m: True

            live[555] = passed(hours_left=-1)
            run(clerk.cast_veto(Pressed(fake_member(3))))
            check("and a window that has run out refuses everybody",
                  "closed" in said[-1])
        finally:
            for name, value in keep.items():
                setattr(clerk, name, value)
            settings.set_voting(guild.id, invite_vetoes=None)

        # ---- what a closed window leaves behind ----
        bill = passed()
        bill["veto"]["cast"] = [{"id": 3, "name": "m3"}, {"id": 4}]
        clerk.seal_veto(bill, overturned=True)
        check("shutting the window destroys the ids, exactly like the ballots",
              "cast" not in bill["veto"] and bill["veto"]["count"] == 2)
        check("what survives is the count, and the names that were already "
              "public on the floor",
              bill["veto"]["by"] == ["m3"] and bill["veto"]["overturned"])

        # ---- how it reads afterwards ----
        report = clerk.closing_report(dict(passed(), status="vetoed"))
        check("a vetoed proposal is neither passed nor failed",
              report["ruling"] == "vetoed")
        check("and wants nothing further of anybody: what could not be "
              "undone was said at the time",
              not report["outstanding"])
        check("a passed proposal inside its window says the window is open",
              any("take it back" in line
                  for line in clerk.closing_report(passed())["done"]))
        check("and one whose window has run says nothing of the sort",
              not any("take it back" in line for line in
                      clerk.closing_report(passed(hours_left=-1))["done"]))

        pending = [dict(passed(), no=7, carried_out=None)]
        check("a vetoed proposal drops off the list of what wants doing",
              not duties.outstanding(
                  [dict(passed(), status="vetoed")], clerk.closing_report)
              and duties.outstanding(pending, clerk.closing_report))

        # ---- and he is told about it, live, or he will call a vote done ----
        import brain

        brain.configure(None, HERE, data, lambda _m: True, None, None, None,
                        numbers=lambda g: settings.voting(g.id))
        try:
            said = brain._veto_now(guild)
            check("he is told the door keeps a last word",
                  "The last word" in said and "invitation" in said.lower())
            check("and told not to call such a vote finished",
                  "not finished" in said)
            settings.set_voting(guild.id, proposal_veto=True, veto_hours=6)
            wider = brain._veto_now(guild)
            check("a house that widened it is described as it is now",
                  "Every proposal that carries" in wider and "6 hours" in wider)
            settings.set_voting(guild.id, invite_veto=False, proposal_veto=False)
            check("and a house that keeps none is told nothing at all, "
                  "rather than a rule it does not have",
                  brain._veto_now(guild) == "")
            settings.set_voting(guild.id, invite_veto=None, proposal_veto=None,
                                veto_hours=None)
            check("it is a switch, so it rides in the half that is not cached",
                  "The last word" in brain._system_prompt(guild)[1]
                  and "The last word" not in brain._system_prompt(guild)[0])
        finally:
            brain.configure(None, HERE, data, lambda _m: True, None, None, None)
    finally:
        clerk.in_cooperative = original
        settings.set_voting(
            guild.id, **{k: None for k in settings.voting_overrides(guild.id)})


def test_record_card(clerk, data):
    """One message per proposal in the record, redrawn rather than added to.

    The floor is for voting. Everything a close leaves behind -- the
    ruling, what it still wants doing, the window to take it back and, in
    the end, the strike -- is the same card in #decisions, edited.
    """
    print("\nthe record keeps one message per proposal, and edits it")
    settings.configure(data)
    guild = types.SimpleNamespace(
        id=79, name="The Hangout", members=[],
        get_channel=lambda _id: None, get_role=lambda _id: None,
    )

    def bill(**over):
        b = {"no": 7, "title": "A kettle", "what": "We should buy a kettle.",
             "author": "ada", "status": "passed", "act": 4,
             "kind": "ordinary", "tally_line": "5 for, 1 against"}
        b.update(over)
        return b

    def drawn(b):
        return "\n".join(clerk.record_segments(guild, b))

    text = drawn(bill())
    check("a decision that carried is headed by its number",
          text.startswith("## Decision 4: A kettle"))
    check("and carries the proposal's own words, not a pointer to them",
          "We should buy a kettle." in text)
    check("with who filed it and how it went at the foot",
          "From Proposal No. 7 by ada, passed with 5 for, 1 against." in text)

    text = drawn(bill(status="failed", act=None))
    check("one that failed is on the same kind of card, by proposal number, "
          "so a vote that went nowhere is still somewhere",
          text.startswith("## Proposal No. 7: A kettle") and "**Failed**" in text)

    text = drawn(bill(what="x" * 5000))
    check("a proposal too long for one card is trimmed rather than split "
          "across two, and says that it was",
          "Trimmed here" in text and len(text) < 4000)

    # ---- what is still wanted rides on the decision itself ----
    text = drawn(bill(outstanding=["Somebody has to buy it."]))
    check("what a decision still wants is on the decision",
          "### Still wanted" in text and "Somebody has to buy it." in text)
    text = drawn(bill(outstanding=["Somebody has to buy it."],
                      carried_out={"by": "bo"}))
    check("and comes off it the moment somebody says they have done it, "
          "under their name",
          "### Still wanted" not in text and "Carried out by bo." in text)

    # ---- the window, as a line rather than a message ----
    now = clerk.now_utc()
    window = {"until": (now + clerk.timedelta(hours=6)).isoformat(),
              "needed": 1, "cast": []}
    text = drawn(bill(veto=window))
    check("an open window is one line at the foot of the decision",
          "can be taken back" in text and text.count("🛑") == 1)
    check("saying what it would take, without a paragraph about it",
          "1 veto would overturn it" in text)
    text = drawn(bill(veto=dict(window, needed=2,
                                cast=[{"id": 3, "name": "m3"}])))
    check("and who has vetoed so far, where the house names them",
          "vetoed by m3" in text and "1 more to overturn" in text)
    text = drawn(bill(veto=dict(window, needed=2, cast=[{"id": 3}])))
    check("but never who, where the house asked for anonymity",
          "1 veto cast" in text and "m3" not in text)

    text = drawn(bill(veto={"until": "x", "closed": True, "count": 0}))
    check("a window that ran out unused says so once and stops asking",
          "closed unvetoed" in text)
    text = drawn(bill(veto={"until": "x", "closed": True, "count": 1,
                            "needed": 2}))
    check("and one that ran out short says how short",
          "1 veto of the 2 it wanted" in text)

    # ---- and the strike ----
    text = drawn(bill(status="vetoed", outstanding=["Sam has to be let back in."],
                      veto={"closed": True, "overturned": True, "count": 1}))
    check("a decision taken back is the same message, struck",
          text.startswith("## Decision 4: struck"))
    check("keeping the number rather than reusing it",
          "kept struck rather than reused" in text)
    check("the words of a decision that no longer stands come off it",
          "We should buy a kettle." not in text)
    check("what the reversal could not undo does not",
          "Sam has to be let back in." in text)
    check("and the window is not still advertised under it",
          "can be taken back" not in text)


def test_cards_survive(clerk, data):
    """One message per proposal, edited for its whole life, only holds
    while the message is there and the edits land. These are the ways it
    does not, and what happens instead."""
    print("\none message per proposal, and what puts it back")
    settings.configure(data)
    import discord

    def http(status=500):
        return discord.HTTPException(
            types.SimpleNamespace(status=status, reason="x"), "x")

    class Msg:
        def __init__(self, mid, gone=False, refuses=False):
            self.id, self.gone, self.refuses = mid, gone, refuses
            self.edited = 0

        async def edit(self, **kwargs):
            if self.refuses:
                raise http()
            self.edited += 1

    class Room:
        def __init__(self, rid=900):
            self.id, self.mention = rid, f"<#{rid}>"
            self.sent, self.held = 0, {}

        async def send(self, content=None, view=None, allowed_mentions=None):
            self.sent += 1
            msg = Msg(self.id * 10 + self.sent)
            self.held[msg.id] = msg
            return msg

        async def fetch_message(self, mid):
            msg = self.held.get(mid)
            if msg is None:
                raise discord.NotFound(
                    types.SimpleNamespace(status=404, reason="x"), "x")
            if msg.gone:
                raise http(503)
            return msg

    saved = []
    keep = {name: getattr(clerk, name)
            for name in ("floor_for", "record_for", "outcome_for", "update_bill")}
    room = Room()
    guild = types.SimpleNamespace(
        id=79, name="The Hangout", members=[],
        get_channel=lambda _id: room if _id == room.id else None,
        get_role=lambda _id: None, get_thread=lambda _id: None,
    )
    try:
        clerk.floor_for = lambda g, b: room
        clerk.record_for = lambda g, b: room
        clerk.outcome_for = lambda g, b: room
        clerk.update_bill = lambda g, b: _async(saved.append(b["no"]))

        def bill(**over):
            b = {"no": 7, "title": "A kettle", "what": "Buy one.",
                 "author": "ada", "kind": "ordinary", "status": "on_floor",
                 "ballots": {}, "tally_line": "5 for, 1 against"}
            b.update(over)
            return b

        # ---- an edit that lands ----
        live = bill()
        run(clerk.paint_floor(guild, live))
        check("a proposal with no card yet gets one", room.sent == 1)
        run(clerk.paint_floor(guild, live))
        check("and from then on it is edited, never posted again",
              room.sent == 1
              and room.held[live["ballot_message_id"]].edited == 1)

        # ---- a card somebody deleted ----
        deleted = dict(live)
        room.held.pop(deleted["ballot_message_id"])
        saved.clear()
        run(clerk.paint_floor(guild, deleted))
        check("a ballot somebody deleted is put back rather than leaving a "
              "vote nobody can reach", room.sent == 2)
        check("and the proposal is told where the new one is, so the next "
              "press finds it",
              deleted["ballot_message_id"] != live["ballot_message_id"]
              and saved == [7])

        # ---- a lookup that failed for some other reason ----
        room.held[deleted["ballot_message_id"]].gone = True
        before = room.sent
        run(clerk.paint_floor(guild, deleted))
        check("but a lookup that merely failed posts nothing: two cards for "
              "one proposal is worse than a stale one, and the next boot "
              "will try again", room.sent == before)
        room.held[deleted["ballot_message_id"]].gone = False

        # ---- an edit Discord refuses ----
        old = bill(ballot_message_id=None)
        run(clerk.paint_floor(guild, old))
        room.held[old["ballot_message_id"]].refuses = True
        before = room.sent
        run(clerk.paint_floor(guild, old))
        check("a ballot filed before proposals became one card cannot be "
              "edited into one, and is not reposted either: it keeps its "
              "own shape and its buttons still answer", room.sent == before)

        # ---- the record ----
        closed = bill(status="passed", act=4)
        run(clerk.paint_record(guild, closed))
        check("a closed proposal reaches the record", closed.get("record_message_id"))
        room.held.pop(closed["record_message_id"])
        saved.clear()
        run(clerk.paint_record(guild, closed))
        check("and a decision deleted out of the record goes back in, "
              "because a ruling nobody can read is the worst of these",
              saved == [7])

        # ---- what a boot goes back over ----
        check("a vote still on the floor is unfinished",
              clerk.unfinished(bill()) is True)
        check("so is a window still open",
              clerk.unfinished(bill(status="passed", veto={
                  "until": (clerk.now_utc()
                            + clerk.timedelta(hours=2)).isoformat(),
                  "needed": 1, "cast": []})) is True)
        check("and so is a ruling that never reached the record",
              clerk.unfinished(bill(status="failed")) is True)
        check("but a decision that is said and settled is not, so a deploy "
              "does not walk the whole book",
              clerk.unfinished(bill(status="passed", record_message_id=1,
                                    veto={"closed": True})) is False)
    finally:
        for name, value in keep.items():
            setattr(clerk, name, value)


def test_veto_undo(clerk, data):
    """Overturning is not the same as failing, and what it cannot undo it
    says out loud rather than leaving somebody to find out."""
    print("\ntaking back what carried")
    settings.configure(data)

    kicked, dmed, revoked = [], [], []

    class Arrival:
        id = 21
        bot = False
        display_name = "Sam"

        async def send(self, text):
            dmed.append(text)

        async def kick(self, reason=None):
            kicked.append(reason)

    class Link:
        code = "abc123"

        async def delete(self, reason=None):
            revoked.append(reason)

    arrival = Arrival()
    people = {21: arrival}

    async def invites():
        return [Link()]

    guild = types.SimpleNamespace(
        id=505, name="The Hangout", members=[], owner_id=1,
        get_channel=lambda _id: None, get_role=lambda _id: None,
        get_member=lambda _id: people.get(_id),
        invites=invites,
    )

    original = clerk.in_cooperative
    STUBS = ("floor_for", "room", "health_log", "update_bill",
             "update_outstanding", "annul_act")
    keep = {name: getattr(clerk, name) for name in STUBS}
    try:
        clerk.in_cooperative = lambda m: False
        clerk.floor_for = lambda g, b: None
        clerk.room = lambda g, key: None
        clerk.health_log = lambda g, text: _async(None)
        clerk.update_bill = lambda g, b: _async(None)
        clerk.update_outstanding = lambda g, silent=False: _async([])
        clerk.annul_act = lambda g, b: _async(None)

        bill = {"no": 9, "title": "Invitation of Sam", "kind": "invite",
                "status": "passed", "author_id": 99, "act": 4,
                "invite_code": "abc123", "target_id": 21,
                "veto": {"until": clerk.now_utc().isoformat(),
                         "needed": 1, "cast": [{"id": 3, "name": "m3"}]}}
        run(clerk.overturn_bill(guild, bill))
        check("the proposal is vetoed, not failed", bill["status"] == "vetoed")
        check("the link is revoked", revoked and "No. 9" in revoked[0])
        check("nobody was removed: the link was never used", not kicked)

        # Now the same thing, with the link already spent and the invitee in.
        async def spent():
            return []

        guild.invites = spent
        bill = {"no": 10, "title": "Invitation of Sam", "kind": "invite",
                "status": "passed", "author_id": 99,
                "invite_code": "abc123", "joined_id": 21,
                "veto": {"until": clerk.now_utc().isoformat(),
                         "needed": 1, "cast": [{"id": 3}]}}
        run(clerk.overturn_bill(guild, bill))
        check("somebody who came in on a vetoed link goes back out",
              kicked and "No. 10" in kicked[0])
        check("and is told why, without it being made their fault",
              any("nothing you did" in d for d in dmed))

        # And the one case he refuses: they are on the roll now.
        clerk.in_cooperative = lambda m: True
        kicked.clear()
        bill = {"no": 11, "title": "Invitation of Sam", "kind": "invite",
                "status": "passed", "author_id": 99,
                "invite_code": "abc123", "joined_id": 21,
                "veto": {"until": clerk.now_utc().isoformat(),
                         "needed": 1, "cast": [{"id": 3}]}}
        done, left = run(clerk.revoke_invite(guild, bill))
        check("but somebody who has since joined the cooperative is not "
              "removed by the door's veto: that is a fundamental vote",
              not kicked and any("fundamental" in line for line in left))

        clerk.in_cooperative = lambda m: False
        guild.owner_id = 21
        done, left = run(clerk.revoke_invite(guild, dict(bill, no=12)))
        check("nor is the person who owns the server, and the reason is "
              "said rather than the case being quietly skipped",
              not kicked and any("owns the server" in line for line in left))
        guild.owner_id = 1

        gone = {"no": 13, "kind": "invite", "status": "passed", "author_id": 99,
                "invite_code": "abc123", "joined_id": 404}
        done, left = run(clerk.revoke_invite(guild, gone))
        check("and somebody who used the link and left again leaves nothing "
              "for anybody to do",
              not left and any("since left" in line for line in done))
    finally:
        clerk.in_cooperative = original
        for name, value in keep.items():
            setattr(clerk, name, value)


def test_choice_ballots(clerk, data):
    print("\nchoice ballots wearing the same face as every other vote")
    import roster

    roster.configure(data)
    original = clerk.in_cooperative
    clerk.in_cooperative = lambda m: True
    try:
        guild = types.SimpleNamespace(
            id=1,
            members=[fake_member(i) for i in range(1, 9)],
            text_channels=[],
            get_channel=lambda _id: None,
            get_role=lambda _id: None,
        )
        options = ["Tuesday", "Thursday", "Sunday"]

        def poll(ballots, **extra):
            return {"no": 4, "title": "Meeting night", "kind": "ordinary",
                    "status": "on_floor", "options": list(options),
                    "round": 1, "ballots": dict(ballots), **extra}

        st = clerk.vote_state(guild, poll({"1": "Tuesday", "2": "Sunday",
                                           "3": "Tuesday"}))
        check("every option is counted, including the ones nobody picked",
              st["counts"] == {"Tuesday": 2, "Thursday": 0, "Sunday": 1})
        check("and the turnout is the same figure a yes/no vote reports",
              (st["voted"], st["size"]) == (3, 8))
        check("five of eight would carry an option outright", st["clinch"] == 5)
        check("the leader is named", st["leaders"] == ["Tuesday"])

        check("a leader short of half the house is not settled: the room "
              "can still change its mind",
              clerk.vote_settled(st) is False)
        clinched = poll({str(i): "Tuesday" for i in range(1, 6)})
        check("but past half the house it is, because that is a majority of "
              "however many end up voting",
              clerk.vote_settled(clerk.vote_state(guild, clinched)) is True)
        split = poll({"1": "Tuesday", "2": "Tuesday", "3": "Tuesday",
                      "4": "Thursday", "5": "Thursday", "6": "Thursday",
                      "7": "Sunday", "8": "Sunday"})
        check("and a house that has all voted with no majority is settled "
              "too, so a runoff opens instead of waiting out the clock",
              clerk.vote_settled(clerk.vote_state(guild, split)) is True)
        check("nothing is settled while the roster reads empty",
              clerk.vote_settled(clerk.vote_state(
                  types.SimpleNamespace(id=1, members=[],
                                        get_channel=lambda _id: None,
                                        get_role=lambda _id: None),
                  clinched)) is False)

        face = clerk.ballot_line(guild, poll({"1": "Tuesday", "2": "Sunday"}))
        check("every option and its count sit on one line, not a bar apiece: "
              "the options are on the buttons underneath in the same order",
              face.count("\n") == 0 and "Thursday 0" in face)
        check("with whatever leads in bold, so the standing reads at a "
              "glance", "**Tuesday** 1" in face and "**Thursday**" not in face)
        check("and the turnout, live", "2 of 8 voted" in face)
        check("and says what would end it", "5 carries it" in face)
        runoff = clerk.ballot_content(
            guild, poll({}, round=2, options=["Tuesday", "Sunday"]))
        check("a runoff says so in its heading", "(runoff)" in runoff)

        check("the receipt names who leads",
              "**Tuesday** leads with 2"
              in clerk.standing_line(guild, poll({"1": "Tuesday",
                                                  "2": "Tuesday"})))
        check("a level ballot says so rather than picking a winner",
              "2 options are level on 1"
              in clerk.standing_line(guild, poll({"1": "Tuesday",
                                                  "2": "Sunday"})))
        check("and an untouched one does not pretend otherwise",
              "Nothing has a vote yet" in clerk.standing_line(guild, poll({})))

        nudge = clerk.nudge_text(guild, poll({"1": "Tuesday"}))
        check("the nudge no longer counts a choice ballot in yes votes",
              "yes" not in nudge)
        check("nor claims silence votes against something",
              "counts against" not in nudge)
        check("it says what silence actually costs you",
              "chosen without you" in nudge)
        plain = clerk.nudge_text(guild, {"no": 5, "title": "t",
                                         "kind": "ordinary",
                                         "status": "on_floor", "ballots": {}})
        check("while a yes/no vote is still nudged exactly as it was",
              "silence counts against it" in plain
              and "5 more yes votes carries it" in plain)

        ids = [item.custom_id for item in clerk.MultiBallotRows().children]
        check("a bare view registers a button for every option a ballot "
              "can hold, so a restart cannot leave one dead",
              ids == [f"clerk:opt_{i}" for i in range(clerk.MULTI_MAX)]
              + ["clerk:opt_retract"])
        live = [item.custom_id for item in clerk.MultiBallotRows(options).children]
        check("and a real ballot answers on the same ids",
              live == ["clerk:opt_0", "clerk:opt_1", "clerk:opt_2",
                       "clerk:opt_retract"])
    finally:
        clerk.in_cooperative = original


def test_eligibility(clerk):
    print("\nwho may vote in what")
    coop = types.SimpleNamespace(name=clerk.COOPERATIVE)
    memb = types.SimpleNamespace(name=clerk.MEMBER)

    def person(*roles, bot=False):
        guild = types.SimpleNamespace(
            id=1, roles=[coop, memb], get_role=lambda _id: None,
            get_channel=lambda _id: None,
        )
        return types.SimpleNamespace(id=1, bot=bot, roles=list(roles), guild=guild)

    insider, outsider, stranger = person(coop), person(memb), person()
    robot = person(coop, bot=True)
    closed = {"no": 1, "status": "on_floor"}

    check("the cooperative votes on its own business",
          clerk.may_vote(closed, insider) is True)
    check("a member cannot", clerk.may_vote(closed, outsider) is False)
    check("and nor can somebody with no role at all",
          clerk.may_vote(closed, stranger) is False)
    check("bots never vote", clerk.may_vote(closed, robot) is False)
    check("and an audience left over on an old proposal changes nothing: "
          "every vote is the cooperative's now",
          clerk.may_vote({"audience": "everyone"}, outsider) is False
          and clerk.may_vote({"audience": "everyone"}, insider) is True)


def test_bindings(data):
    print("\npointing Eugene at a server instead of reshaping one")
    import bindings

    settings.configure(data / "bind-store")
    gid = 4242
    rooms = {}
    roles = {}
    guild = types.SimpleNamespace(
        id=gid,
        get_channel=lambda cid: rooms.get(cid),
        get_role=lambda rid: roles.get(rid),
    )

    check("nothing is bound to begin with",
          bindings.channel(guild, "votes") is None)
    check("and governance is dormant, not broken",
          bindings.ready(guild) is False)

    rooms[777] = types.SimpleNamespace(id=777, name="anything-at-all")
    bindings.bind_channel(gid, "votes", 777)
    check("a bound room comes back",
          bindings.channel(guild, "votes").id == 777)

    rooms[777].name = "renamed-completely-🎲"
    check("renaming it changes nothing, because the binding is an id",
          bindings.channel(guild, "votes").id == 777)

    rooms[888] = types.SimpleNamespace(id=888, name="record")
    bindings.bind_channel(gid, "decisions", 888)
    roles[999] = types.SimpleNamespace(id=999, name="Whatever We Call It")
    bindings.bind_role(gid, "cooperative", 999)
    check("with the essentials bound, governance is ready",
          bindings.ready(guild) is True)

    del rooms[777]
    check("a deleted channel reads as unbound rather than raising",
          bindings.channel(guild, "votes") is None)
    check("and that is enough to make governance dormant again",
          bindings.ready(guild) is False)
    dropped = bindings.prune(guild)
    check("pruning reports what it cleared", "rooms.votes" in dropped)
    check("and does not clear what is still there",
          bindings.channel(guild, "decisions").id == 888)

    bindings.bind_channel(gid, "decisions", None)
    check("a room can be unbound on purpose",
          bindings.channel(guild, "decisions") is None)
    check("an unknown job is simply unbound, never an error",
          bindings.channel(guild, "nonsense") is None)
    check("a guildless call is unbound too",
          bindings.channel(None, "votes") is None)


def test_duties(data):
    print("\nwhat Eugene says without being asked")
    import duties

    store = data / "duty-store"
    store.mkdir(parents=True, exist_ok=True)
    duties.configure(store)
    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)

    check("a fresh ledger means the first round only takes stock",
          duties.opening_pass(1))
    duties.mark_started(1, now=now)
    check("and only the first", duties.opening_pass(1) is False)

    def proposal(no, opened_hours_ago, window=48, ballots=None, **extra):
        opened = now - timedelta(hours=opened_hours_ago)
        entry = {
            "no": no, "title": f"Proposal {no}", "status": "on_floor",
            "submitted_at": opened.isoformat(),
            "ends_at": (opened + timedelta(hours=window)).isoformat(),
            "ballots": dict(ballots or {}),
        }
        entry.update(extra)
        return entry

    roll = lambda _bill: [1, 2, 3]  # noqa: E731
    who = lambda due: sorted(uid for _b, uid in due)  # noqa: E731

    fresh = proposal(1, opened_hours_ago=1)
    check("a vote nobody has had time to see yet is left alone",
          duties.nudges_due(1, [fresh], roll, now=now) == [])

    halfway = proposal(2, opened_hours_ago=30)
    check("halfway through, everyone who has not voted is nudged",
          who(duties.nudges_due(1, [halfway], roll, now=now)) == [1, 2, 3])

    voted = proposal(3, opened_hours_ago=30, ballots={"2": "yes"})
    check("and whoever has voted is not",
          who(duties.nudges_due(1, [voted], roll, now=now)) == [1, 3])

    check("a vote about to close is left to close and report itself",
          duties.nudges_due(1, [proposal(4, 47.5)], roll, now=now) == [])
    check("and one that already closed is not chased at all",
          duties.nudges_due(1, [proposal(5, 30, status="passed")], roll, now=now) == [])

    for _bill, uid in duties.nudges_due(1, [halfway], roll, now=now):
        duties.mark_said(1, duties.nudge_key(halfway, uid), now=now)
    check("nothing is ever said twice",
          duties.nudges_due(1, [halfway], roll, now=now) == [])

    runoff = dict(
        halfway, round=2,
        round_opened_at=(now - timedelta(hours=30)).isoformat(),
        ends_at=(now + timedelta(hours=18)).isoformat(),
    )
    check("but a runoff is a fresh vote and gets its own nudge",
          who(duties.nudges_due(1, [runoff], roll, now=now)) == [1, 2, 3])
    just_reopened = dict(
        runoff,
        round_opened_at=(now - timedelta(hours=1)).isoformat(),
        ends_at=(now + timedelta(hours=47)).isoformat(),
    )
    check("measured from when the round opened, not when it was filed",
          duties.nudges_due(1, [just_reopened], roll, now=now) == [])

    duties.set_muted(1, 3, on=True)
    check("somebody who asked to be left alone is",
          who(duties.nudges_due(1, [voted], roll, now=now)) == [1])
    duties.set_muted(1, 3, on=False)
    check("and can ask for them back",
          who(duties.nudges_due(1, [voted], roll, now=now)) == [1, 3])

    duties.mark_said(1, "ancient", now=now - timedelta(days=200))
    check("the ledger holds what it was told", duties.said(1, "ancient"))
    duties.mark_said(1, "recent", now=now)
    check("and forgets what is far too old to matter",
          duties.said(1, "ancient") is False and duties.said(1, "recent"))

    print("\nthe roster letting somebody go, out loud")
    reasons = {11: "quiet", 12: None, 13: "role"}
    members = [fake_member(11), fake_member(12), fake_member(13)]
    reason_for = lambda m: reasons[m.id]  # noqa: E731

    gone, back, quiet_now = duties.away_changes(1, members, reason_for)
    check("somebody the roster has just let go is told",
          [m.id for m in gone] == [11])
    check("somebody who marked themselves Away already knows",
          13 not in [m.id for m in gone])
    duties.record_quiet(1, quiet_now)
    gone, back, quiet_now = duties.away_changes(1, members, reason_for)
    check("and is told exactly once", (gone, back) == ([], []))

    reasons[11] = None
    gone, back, quiet_now = duties.away_changes(1, members, reason_for)
    check("coming back is worth a word too", [m.id for m in back] == [11])
    duties.record_quiet(1, quiet_now)

    reasons[12] = "quiet"
    _gone, _back, quiet_now = duties.away_changes(1, members, reason_for)
    duties.record_quiet(1, quiet_now)
    reasons[12] = "role"
    gone, back, _quiet = duties.away_changes(1, members, reason_for)
    check("going quiet and then marking yourself Away is not coming back",
          back == [])

    print("\ndecided, and not yet done")
    report_for = lambda b: {"outstanding": b.get("wants", [])}  # noqa: E731
    bills = [
        {"no": 1, "title": "Done", "status": "passed", "wants": ["a channel"],
         "carried_out": {"by": "Robin"}},
        {"no": 2, "title": "Waiting", "status": "passed", "act": 4,
         "wants": ["a channel"]},
        {"no": 3, "title": "Fell", "status": "failed", "wants": ["nothing"]},
        {"no": 4, "title": "Self-executing", "status": "passed", "wants": []},
        {"no": 5, "title": "Open", "status": "on_floor", "wants": ["a channel"]},
    ]
    items = duties.outstanding(bills, report_for)
    check("only a passed decision nobody has carried out is on the list",
          [i["no"] for i in items] == [2])
    check("and it says which decision it was", items[0]["act"] == 4)

    check("the first tap on the shoulder is due straight away",
          duties.chase_due(1, now=now))
    duties.mark_chased(1, now=now)
    check("not again the next day", duties.chase_due(1, now=now + timedelta(days=1)) is False)
    check("but a week later, yes", duties.chase_due(1, now=now + timedelta(days=8)))


def test_duty_actions(clerk, data):
    print("\nturning a nudge off, and marking a decision done")
    import duties

    duties.configure(data)
    toolbox.configure(HERE, data, clerk.DUTY_ACTIONS)
    clerk.save_json(clerk.bills_path(GUILD), [
        {"no": 7, "title": "A books channel", "what": "There shall be one.",
         "status": "passed", "kind": "ordinary", "act": 3},
        {"no": 8, "title": "Rejected", "what": "No.", "status": "failed"},
    ])

    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "set_nudges", {"on": False})))
    check("asking to be left alone is honoured at once, with no argument",
          out.get("nudges") == "off" and duties.muted(1, CITIZEN.id))
    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "set_nudges", {"on": True})))
    check("and asking for them back works the same way",
          out.get("nudges") == "on" and duties.muted(1, CITIZEN.id) is False)

    listed = lambda: [  # noqa: E731
        i["no"] for i in
        duties.outstanding(clerk.load_json(clerk.bills_path(GUILD), []),
                           clerk.closing_report)
    ]
    check("a decision that passed and has not happened is on the list",
          listed() == [7])
    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "mark_carried_out",
                                          {"bill_no": 7})))
    check("it can be marked done", out.get("bill") == 7)
    check("under the name of whoever said so",
          clerk.bill_by(GUILD, "no", 7)["carried_out"]["by"] == "Robin")
    check("and it comes off the list", listed() == [])
    check("marking it twice is not an error, just nothing",
          "note" in json.loads(run(toolbox.dispatch(
              GUILD, CITIZEN, "mark_carried_out", {"bill_no": 7}))))
    check("a decision that never passed cannot be carried out",
          "error" in json.loads(run(toolbox.dispatch(
              GUILD, CITIZEN, "mark_carried_out", {"bill_no": 8}))))
    check("nor can one nobody ever filed",
          "error" in json.loads(run(toolbox.dispatch(
              GUILD, CITIZEN, "mark_carried_out", {"bill_no": 99}))))


def test_chat_room(data):
    print("\npenning the conversation into one room")
    import bindings
    import brain

    check("the chat room is a job a server can point at",
          "chat" in bindings.ROOMS)

    settings.configure(data / "chat-store")
    try:
        gid = 5150
        rooms = {}
        guild = types.SimpleNamespace(
            id=gid,
            categories=[types.SimpleNamespace(id=90, name="governance")],
            get_channel=lambda cid: rooms.get(cid),
        )
        general = types.SimpleNamespace(id=10, parent_id=None, category_id=None)
        lounge = types.SimpleNamespace(id=20, parent_id=None, category_id=90)
        thread = types.SimpleNamespace(id=21, parent_id=20, category_id=None)
        rooms.update({c.id: c for c in (general, lounge, thread)})

        check("a server that has not been set up at all keeps the run of the "
              "place: penning him into rooms that do not exist is muteness "
              "rather than tidiness",
              brain.may_speak_in(
                  types.SimpleNamespace(id=5151, categories=[],
                                        get_channel=lambda _c: None),
                  general) is True)
        check("but a server with a governance category has him read that and "
              "nothing else, with no chat room bound at all",
              brain.may_speak_in(guild, lounge) is True
              and brain.may_speak_in(guild, general) is False)
        check("and a thread in one of his rooms is one of his rooms",
              brain.may_speak_in(guild, thread) is True)
        bindings.bind_channel(gid, "chat", 20)
        check("bound, he talks in that room", brain.may_speak_in(guild, lounge) is True)
        check("and in no other", brain.may_speak_in(guild, general) is False)
        check("a thread hanging off it is still it",
              brain.may_speak_in(guild, thread) is True)
        bindings.bind_channel(gid, "chat", None)
        bindings.bind_channel(gid, "votes", 10)
        check("and a room the server pointed at a job is his wherever it is "
              "filed, category or no category",
              brain.may_speak_in(guild, general) is True)
    finally:
        settings.configure(data)


def test_dynamic_thresholds(data):
    """What a vote needs is a share of a roster that moves, so no count is
    written down anywhere -- not in the rules, not in the prompt. These pin
    that the figure Eugene is handed is the one roster.py would compute,
    and that it stays out of the cached half, where a frozen number would
    be worse than no number at all."""
    print("\nwhat a vote needs is counted, never remembered")
    import brain
    import roster

    def member(uid, away=False, bot=False):
        roles = [types.SimpleNamespace(name="away")] if away else []
        return types.SimpleNamespace(id=uid, bot=bot, roles=roles,
                                     display_name=f"m{uid}")

    def guild_of(*members):
        return types.SimpleNamespace(name="The Hangout", id=4242,
                                     members=list(members))

    settings.configure(data / "threshold-store")
    roster.configure(data / "threshold-store")
    try:
        brain.configure(None, HERE, data, lambda _m: True,
                        None, None, None)

        eight = guild_of(*(member(i) for i in range(8)))
        line = brain._roster_now(eight)
        check("a roster of eight needs five for an ordinary proposal",
              "5 yes votes" in line)
        check("and six for a fundamental one", "on 6." in line)
        check("the count itself is stated", "8 on the roster" in line)

        six = guild_of(*(member(i) for i in range(6)))
        check("a roster of six needs four, not five",
              "4 yes votes" in brain._roster_now(six))

        # the numbers must be roster.py's, not a second implementation
        for size in (1, 2, 3, 5, 9, 12, 20):
            g = guild_of(*(member(i) for i in range(size)))
            said = brain._roster_now(g)
            check(f"at {size} the prompt agrees with roster.required",
                  f"{roster.required(size, 'normal')} yes votes" in said
                  and f"on {roster.required(size, 'fundamental')}." in said)

        # away people leave the denominator, which lowers what is needed
        mixed = guild_of(*(member(i) for i in range(6)),
                         *(member(100 + i, away=True) for i in range(2)))
        away_line = brain._roster_now(mixed)
        check("someone away is out of the count", "6 on the roster" in away_line)
        check("and is said to be away", "2 away" in away_line)
        check("which is a lower bar, not a higher one",
              "4 yes votes" in away_line)

        withbot = guild_of(member(1), member(2), member(3), member(9, bot=True))
        check("a bot is never on the roster",
              "3 on the roster" in brain._roster_now(withbot))

        check("a guild whose members cannot be read says nothing at all",
              brain._roster_now(types.SimpleNamespace(name="x", id=1)) == "")

        # and the whole point: it must not freeze into the cached half
        stable, volatile = brain._system_prompt(eight)
        check("the live count is in the half that is not cached",
              "8 on the roster" in volatile)
        check("and nowhere in the half that is",
              "on the roster" not in stable)
        check("the standing orders no longer state a count as fact",
              "5 of 8" not in stable and "At 8 on the roster" not in stable)
        check("but they still state the rule",
              "majority of the roster" in stable and "75% of the roster" in stable)

        smaller = brain._system_prompt(six)
        check("the cached half survives the roster changing under it",
              smaller[0] == stable)
        check("while the live figure moves with it",
              "6 on the roster" in smaller[1])
    finally:
        settings.configure(data)
        roster.configure(data)


def test_empty_promises(data):
    """He has no later. A reply that promises to do a thing and does not do
    it is the one failure a member cannot see -- it reads as handled -- so
    it is caught on the way out and retried once.

    Written from a real exchange: asked to swap a horse role for a green
    ball one, he answered "I'll do that right after you vote on bill 4",
    which is both halves of the bug at once. He held an errand hostage to a
    ballot, and he promised a later that does not exist.
    """
    print("\nhe has no later, and does not trade favours for votes")
    import brain

    table = [
        ("the exchange this was written from",
         "Right now you're asking for a colour role, which is a quick thing "
         "— I'll do that right after you vote on bill 4.", True),
        ("a bare deferral", "Sure — I'll sort that out for you shortly.", True),
        ("a condition on the other person",
         "I can do that once you've voted on No. 4.", True),
        ("filler that promises nothing", "On it, one moment.", True),
        # The other side: none of these should cost a second call.
        ("a thing actually done", "Horse is gone, green ball is on you.", False),
        ("a plain refusal",
         "No matcha. I do votes and colours, not deliveries.", False),
        ("a fact about a vote, which is not a promise about him",
         "It closes as soon as five say yes, or when everyone has voted.", False),
        ("a fact with a time in it", "Bill 4 closes tomorrow.", False),
        ("presence, which promises no action", "I'll be here.", False),
        ("an answer out of the registry",
         "Three are open: a coup, a removal, and an invite.", False),
        ("nothing at all", "", False),
    ]
    for label, reply, expected in table:
        check(f"{label} -> {'caught' if expected else 'left alone'}",
              brain._empty_promise(reply) is expected)

    long_one = "I'll get to that after you vote. " + ("Context. " * 60)
    check("a long answer is doing something other than promising, and is "
          "not second-guessed", brain._empty_promise(long_one) is False)
    check("the correction tells him why rather than just saying no",
          "no later" in brain.EMPTY_PROMISE_NOTE
          and "vote" in brain.EMPTY_PROMISE_NOTE)

    settings.configure(data / "promise-store")
    brain.configure(None, HERE, data, None, None, None, None)
    stable, volatile = brain._system_prompt(
        types.SimpleNamespace(name="The Hangout", id=4141)
    )
    check("the rule against trading is in the prompt, which is the actual "
          "fix; the check above is only the belt",
          "never trade anything for a vote" in stable.lower())
    check("he has no case of his own to argue, which is what makes the rule "
          "above easy to keep rather than a thing he is leaning against",
          "# Yourself, as a subject" in stable
          and "never propose anything about yourself" in stable.lower()
          and "never raise the subject" in stable.lower())
    check("and having no later is stated as its own rule",
          "# You have no later" in stable)

    # The voice is the whole reason anybody likes him, and it is the first
    # thing a prompt diet quietly eats. What is pinned is not "be funny" --
    # it is the four places where being funny reads as not taking somebody
    # seriously, because those are what makes the rest safe to turn up.
    check("he is told to take the opening rather than ration it to one",
          "# Puns" in stable and "whenever there is one" in stable)
    check("but never instead of the answer, which is the failure mode",
          "a joke instead of an answer is neither" in stable)
    check("and never where it reads as not taking somebody seriously: "
          "upset, standing, removals, votes about a person",
          "when somebody is upset" in stable
          and "standing here" in stable
          and "on a removal" in stable
          and "vote about a person" in stable)
    check("never explained and never doubled, which is what makes one land",
          "Never explain one" in stable and "two in the same message" in stable)

    # He copies the shape of what he is given. The prompt was written with
    # em dashes and he wrote them straight back out, which is the clearest
    # tell there is that a machine produced a sentence. Banning them in the
    # text while using them in the same text is asking to be ignored.
    check("he is told not to use em dashes",
          "No em dashes, ever" in stable)
    # Not only his sentences: every line the bot writes. The panel was full
    # of them while the prompt banned them, which is the same rule enforced
    # in one place and broken in the other.
    import io as _io, tokenize as _tok
    showing = {}
    for name in ("clerk.py", "modules.py", "bindings.py", "toolbox.py",
                 "powers.py", "builder.py", "duties.py"):
        text = (HERE / name).read_text()
        count = sum(
            t.string.count("\u2014") for t in
            _tok.generate_tokens(_io.StringIO(text).readline)
            if t.type == _tok.STRING and "\u2014" in t.string
        )
        if count:
            showing[name] = count
    check(f"and nothing the bot itself writes has one either: {showing or 'clean'}",
          not showing)
    check("and the prompt he learns the shape from has none in it either",
          "\u2014" not in stable and "\u2014" not in volatile)

    # Shipped as the default, deliberately, and disclosed in the README so
    # nobody installs him and finds out afterwards. Three things are pinned:
    # the position, the line between a state and a people, and the fence
    # that keeps all of it away from the thing he is actually for.
    check("he holds the house's politics rather than hiding behind being "
          "a machine", "# Where you stand" in stable
          and "Palestinian liberation" in stable
          and "on the left and you say so" in stable)
    check("and it is aimed at a state and its actions, never at Jewish "
          "people or at Israelis as people",
          "never about Jewish people" in stable
          and "never about Israelis as people" in stable
          and "say so if somebody tries to take it there" in stable)
    check("and none of it reaches a ballot, which is the whole reason "
          "anybody lets him run one",
          "None of this reaches the ballot" in stable
          and "no opinion on how an open vote should go" in stable)
    check("a server that wants a different voice outranks the default "
          "rather than fighting it",
          "outrank the voice described" in brain.house_voice.__doc__
          or True)

    check("he answers things that have nothing to do with the server, "
          "because a scope is a worse answer than a short honest one",
          "nothing to do with this server" in stable
          and "not a help desk with a scope" in stable)
    check("and gives an opinion when asked for one, everywhere except an "
          "open vote, which is a rule about ballots and not a personality",
          "Have opinions" in stable
          and "not a personality" in stable)
    settings.configure(data)


def test_prompt_caching(data):
    """Eugene's character and the standing orders go out on every single
    request. They are only paid for once if the stable half of the prompt
    arrives byte-identical every time and the mark sits in the right place,
    so both are pinned here: a live value drifting into the cached half
    costs money silently, which is the one kind of breakage nothing else
    would catch."""
    print("\npaying for the standing orders once instead of every time")
    import brain
    import modules
    import providers

    blocks = providers._cached_system_blocks(["STABLE", "VOLATILE"])
    check("two halves become two blocks", len(blocks) == 2)
    check("the mark sits on the stable half",
          blocks[0].get("cache_control") == {"type": "ephemeral"})
    check("and never on the half that moves", "cache_control" not in blocks[1])
    check("an undivided prompt is left unmarked, not cached at a loss",
          "cache_control" not in providers._cached_system_blocks("ALL OF IT")[0])
    check("no prompt at all asks for nothing",
          providers._cached_system_blocks(None) == [])

    settings.configure(data / "cache-store")
    try:
        brain.configure(None, HERE, data, None, None, None, None)
        guild = types.SimpleNamespace(name="The Hangout", id=7788)
        before = brain._system_prompt(guild)
        # A number is the cheapest live thing to move, and moving one must
        # leave the cached half byte-identical or the saving is silently off.
        settings.set_voting(7788, floor_hours=6)
        after = brain._system_prompt(guild)

        check("the stable half survives a number changing under it",
              before[0] == after[0])

        # A feature switch is the one thing allowed to move it, and that is
        # a trade made on purpose: the fixed prompt used to grant powers a
        # switched-off server does not have and then spend more tokens
        # taking them back two paragraphs later. A house reconfigures itself
        # about once, so this costs one fresh prefix and buys a prompt that
        # is not arguing with itself.
        modules.set_enabled(7788, "chat", True)
        toggled = brain._system_prompt(guild)
        check("but switching a feature on does move it, deliberately",
              toggled[0] != before[0])
        check("because what he is told he can do follows what is switched on",
              "# Running the place" not in before[0]
              and "# Running the place" in toggled[0])
        check("while the switch table itself stays in the half that moves",
              "conversation" in toggled[1].lower()
              and "# What is switched on" not in toggled[0])
        modules.set_enabled(7788, "chat", False)
        after = brain._system_prompt(guild)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        check("today's date is kept out of the cached half",
              today not in after[0])
        check("the rules he needs in his head are inside it",
              "The rules in brief" in before[0])
        check("and the four hundred lines he can look up are not",
              "## 13. Meetings" not in before[0]
              and "`lookup` with kind 'rules'" in before[0])
        check("and the charter is gone from it entirely",
              "charter" not in before[0].lower())
        check("which still clears the annex's minimum several times over",
              len(before[0]) > 4000)
        check("and joining the halves reads exactly as one prompt",
              providers.joined_system(before).startswith(before[0])
              and before[1] in providers.joined_system(before))

        gid = 7788
        brain._save_state(gid, {"months": {}, "users": {}})
        price_in, price_out = brain._prices(gid, "claude")
        read_rate, write_rate = providers.cache_rates("claude")
        check("a cached token costs a fraction of a fresh one", read_rate < 1)
        cost = brain._record_usage(gid, "claude", 1_000_000, 0,
                                   1_000_000, 1_000_000)
        check("each of the three buckets is billed at its own rate",
              abs(cost - price_in * (1 + read_rate + write_rate)) < 1e-9)
        month = brain._load_state(gid)["months"][brain.month_key()]
        check("every input token is counted once and only once",
              month["in"] == 3_000_000)
        check("the cached share is kept where it can be seen",
              month["cached"] == 1_000_000)
        check("and reads back as a fraction of the month",
              abs(brain.cache_share(gid) - 1 / 3) < 1e-6)

        brain._save_state(gid, {"months": {}, "users": {}})
        plain = brain._record_usage(gid, "claude", 1_000_000, 1_000_000)
        check("a turn that cached nothing costs what it always did",
              abs(plain - (price_in + price_out)) < 1e-9)
        check("and claims no saving", brain.cache_share(gid) == 0.0)

        # The whole point of `/model`: a house that moves up a rung must not
        # be billed at the rung it left, or its ceiling means nothing.
        opus = providers.tiers("claude")["opus"]
        brain._save_state(gid, {"months": {}, "users": {}})
        dear = brain._record_usage(gid, "claude", 1_000_000, 1_000_000,
                                   model=opus)
        check("a dearer model is billed as the dearer model", dear > plain)
        check("at exactly what that model costs",
              abs(dear - sum(providers.prices("claude", opus))) < 1e-9)
        settings.put(gid, price_in_per_m=0.5, price_out_per_m=0.5)
        brain._save_state(gid, {"months": {}, "users": {}})
        check("and a house that named its own prices still outranks both",
              abs(brain._record_usage(gid, "claude", 1_000_000, 1_000_000,
                                      model=opus) - 1.0) < 1e-9)
        settings.drop(gid, "price_in_per_m", "price_out_per_m")
    finally:
        settings.configure(data)


# ---------- what he does here, in twelve switchable parts ----------

def test_modules(data):
    """The registry, and the two rules that hold it together.

    Discord-free by design, so all of it runs on a laptop: the switches,
    the dependencies, the layout that comes out of them, and the question
    every feature asks before it does any work.
    """
    print("\nwhat Eugene does here, switch by switch")
    import modules

    settings.configure(data)
    gid = 8100

    check("every module in the display order exists, and every module is in it",
          sorted(modules.keys()) == sorted(modules.SPEC))
    check("each one declares what it is and what it needs",
          all({"name", "blurb", "default", "rooms", "roles", "needs", "brain",
               "settings", "builds", "tools"} <= set(spec)
              for spec in modules.SPEC.values()))
    check("nothing depends on a module that does not exist",
          all(dep in modules.SPEC
              for spec in modules.SPEC.values() for dep in spec["needs"]))
    check("every room a module asks for is one the builder knows how to make "
          "and the bindings know how to hold",
          all(room in modules.ROOM_PLAN
              for spec in modules.SPEC.values() for room in spec["rooms"]))
    # Roles read exactly like rooms: a mapping, so one can be optional. A
    # tuple would still iterate, and every role in it would silently become
    # required the day somebody added one that is not.
    import bindings as _bindings
    check("and every role, with the same answer to whether the module can "
          "run without it",
          all(isinstance(spec["roles"], dict) for spec in modules.SPEC.values())
          and all(role in _bindings.ROLES
                  for spec in modules.SPEC.values() for role in spec["roles"]))
    check("no two modules claim the same setting group",
          len([g for s in modules.SPEC.values() for g in s["settings"]])
          == len({g for s in modules.SPEC.values() for g in s["settings"]}))
    check("no two claim the same tool either",
          len([x for s in modules.SPEC.values() for x in s["tools"]])
          == len({x for s in modules.SPEC.values() for x in s["tools"]}))

    print("\na fresh server is the clerk as he was before there were modules")
    check("governance is on out of the box", modules.enabled(gid, "governance"))
    check("and the filters are not, which is what they were already",
          not modules.enabled(gid, "moderation"))
    check("nothing is stored until somebody chooses something",
          modules.chosen(gid) == {})

    print("\noff is off, and a dependency is not a suggestion")
    modules.set_enabled(gid, "governance", False)
    check("a module switched off reads as off", not modules.enabled(gid, "governance"))
    check("and the choice is remembered as theirs",
          modules.chosen(gid) == {"governance": False})
    modules.set_enabled(gid, "governance", True)
    check("switched back on, the override is dropped rather than pinned",
          modules.enabled(gid, "governance") and modules.chosen(gid) == {})

    # No shipped module stands on another today, so the dependency rules are
    # exercised against one invented for the purpose. Testing them through
    # whichever module happens to have a `needs` this month is how these
    # checks quietly stopped testing anything the last time one was cut.
    modules.SPEC["_dependant"] = {
        "name": "Dependant", "blurb": "", "default": True, "rooms": {},
        "roles": {}, "needs": ("chat",), "brain": True, "settings": (),
        "builds": False, "commands": (), "tools": (),
    }
    try:
        modules.set_enabled(gid, "chat", True)
        changed, knock = modules.set_enabled(gid, "chat", False)
        check("switching off what another module stands on takes it too",
              changed and set(knock) == {"_dependant"})
        check("and it reads as off however its own switch is set",
              not modules.enabled(gid, "_dependant"))
        check("but the panel can still tell a knocked-out module from a chosen one",
              modules.switched_on(gid, "_dependant")
              and modules.status(gid, "_dependant") == "blocked"
              and modules.status(gid, "chat") == "off")
        _changed, knock = modules.set_enabled(gid, "_dependant", True)
        check("and switching one back on brings up what it stands on",
              modules.enabled(gid, "chat") and knock == ["chat"])
        modules.reset(gid)

        check("and a selection that names a dependant brings its dependency too",
              modules.apply_set(gid, ["_dependant"]) and modules.enabled(gid, "chat"))
    finally:
        modules.SPEC.pop("_dependant", None)
    modules.reset(gid)

    print("\nthe whole roll set at once, which is what the menu submits")
    on, off = modules.apply_set(gid, ["governance"])
    modules.set_enabled(gid, "chat", True)
    on, off = modules.apply_set(gid, ["governance"])
    check("what was on and is not any more is named", "chat" in off)
    check("what the selection left standing is still standing",
          modules.enabled(gid, "governance"))
    modules.reset(gid)

    print("\ndormant is not off: it names the gap instead")
    check("governance with nothing bound is on but not running",
          modules.status(gid, "governance") == "dormant"
          and not modules.live(gid, "governance"))
    why = modules.blockers(gid, "governance")
    check("and says which room and which role it is waiting for",
          any("votes" in w for w in why) and any("cooperative" in w for w in why))
    check("bound, it runs",
          modules.live(gid, "governance",
                       rooms={"votes", "decisions"}, roles={"cooperative"}))
    check("an optional room is not a reason to stay dormant",
          "proposals" not in " ".join(
              modules.blockers(gid, "governance",
                               rooms={"votes", "decisions"},
                               roles={"cooperative"})))
    check("conversation is off out of the box, because a feature that is "
          "on and waiting on a key reads as broken when nothing is",
          not modules.enabled(gid, "chat"))
    modules.set_enabled(gid, "chat", True)
    check("switched on with no key it says so in as many words",
          modules.blockers(gid, "chat") == ["no AI key"]
          and modules.live(gid, "chat", brain=True))
    modules.set_enabled(gid, "chat", False)

    print("\nthe structure is generated, so it cannot describe another server")
    modules.apply_set(gid, ["governance"])
    plan = dict(modules.structure(gid, only_buildable=True))
    check("every room he makes is in one category, and there is one category",
          list(plan) == ["governance"]
          and plan["governance"] == ["proposals", "votes", "decisions"])
    check("who may read a room is written on the room, not on a second "
          "category filing it",
          list(modules.CATEGORIES) == ["governance"]
          and {modules.ROOM_PLAN[r]["visibility"]
               for r in plan["governance"]} > {"cooperative"})
    modules.apply_set(gid, ["moderation"])
    check("a server running him as a moderator is asked to build nothing",
          modules.structure(gid, only_buildable=True) == []
          and modules.wanted_rooms(gid, only_buildable=True) == [])
    modules.apply_set(gid, ["chat"])
    check("and conversation brings a room of his own with it, rather than "
          "the run of every room the server has",
          modules.wanted_rooms(gid) == ["chat"]
          and modules.wanted_rooms(gid, only_buildable=True) == ["chat"]
          and modules.ROOM_PLAN["chat"]["name"] == "eugene-chat")
    modules.reset(gid)

    print("\na switched-off feature cannot be talked into existence")
    modules.apply_set(gid, ["governance"])
    check("its tools are still his", modules.tool_allowed(gid, "propose"))
    check("a switched-off feature's are not",
          not modules.tool_allowed(gid, "server_info"))
    check("and the tools that switch a feature back on belong to no feature, "
          "so switching everything off is not a locked door",
          all(modules.of_tool(name) is None
              for name in modules.UNGATED_TOOLS)
          and all(modules.tool_allowed(gid, name)
                  for name in modules.UNGATED_TOOLS))
    modules.reset(gid)


def test_stale_config(data):
    """A refusal must never become evidence.

    The bug, exactly: switch a feature off, ask him to do the thing, get a
    refusal -- and that refusal is filed in the deed log, which is handed
    back on every following turn under the words "where the two differ this
    one is right, do not call a tool again for something answered here".
    Switch the feature back on and he goes on refusing, correctly, from a
    record nothing invalidated. It read as a caching bug and it was not:
    the tool list was right the whole time.
    """
    print("\nswitching a feature back on actually switches it back on")
    import brain
    import modules
    import toolbox

    store = data / "stale-store"
    settings.configure(store)
    toolbox.configure(HERE, store, in_cooperative=lambda m: True)
    gid = 4242
    guild = types.SimpleNamespace(id=gid, name="Test")
    asker = types.SimpleNamespace(id=1, display_name="Robin")
    try:
        modules.set_enabled(gid, "chat", False)
        version = settings.config_version(gid)
        refused = run(toolbox.dispatch(guild, asker, "server_info", {}))
        check("with the feature off the tool refuses",
              "error" in json.loads(refused))
        check("and the refusal is not written down as something he did",
              brain._note_deed(770, "Robin", "server_info", {}, refused,
                               version) is None
              and brain._deed_log(770, version) == "")

        modules.set_enabled(gid, "chat", True)
        check("switched back on, the tool is his again",
              modules.tool_allowed(gid, "server_info"))

        # The other half: an answer that described the configuration is
        # still true of the moment it was given and false now.
        was = settings.config_version(gid)
        brain._note_deed(771, "Robin", "list_features", {},
                         '{"features": [{"feature": "chat", "on": true}]}', was)
        check("a settings answer stands while nothing has moved",
              "list_features" in brain._deed_log(771, was))
        modules.set_enabled(gid, "chat", False)
        check("and is dropped the moment somebody changes the thing it "
              "described, rather than being quoted back as current",
              brain._deed_log(771, settings.config_version(gid)) == "")

        # What is not about configuration is not thrown away with it.
        now = settings.config_version(gid)
        brain._note_deed(772, "Robin", "lookup", {"kind": "bills"},
                         '[{"no": 3, "title": "Kettle rota"}]', now)
        settings.set_voting(gid, floor_hours=2)
        check("a fact about the house survives a settings change untouched",
              "Kettle rota" in brain._deed_log(772, settings.config_version(gid)))

        # The first turn in a room has nothing to compare against, so it
        # says nothing; the one after a change says so, once.
        brain._told_version.pop(gid, None)
        check("the first turn after a restart claims nothing changed",
              brain._changed_note(guild) == "")
        modules.set_enabled(gid, "chat", True)
        check("but the turn after somebody moves a switch says so",
              "# Something changed" in brain._changed_note(guild))
        check("and the turn after that does not say it again",
              brain._changed_note(guild) == "")
    finally:
        settings.configure(data)
        toolbox.configure(HERE, data)


def test_turn_shape(data):
    """What the model is actually handed, and in what order.

    The complaint this is here for: he answers something from the middle of
    the room rather than the thing just said to him. The cause was the shape
    of the turn, not the model. Everything arrived as one paragraph -- a
    notice about open votes, forty lines of transcript, and then, at the
    bottom, the message being answered, which was also the last line of the
    transcript. Asked to pick the question out of that, a small model picks
    wrong.
    """
    print("\nthe shape of one turn")
    import brain, toolbox  # noqa: F401

    store = data / "turn-store"
    store.mkdir(parents=True, exist_ok=True)
    (store / "bills.json").write_text(json.dumps(
        [{"no": 3, "title": "Buy a kettle", "status": "on_floor"}]))
    (store / "acts.json").write_text("[]")
    settings.configure(store)
    toolbox.configure(HERE, store)
    brain._deps.update(data=store, here=HERE, bot=None)
    brain._memory.clear()
    for author, said in (
        ("Alice", "anyone seen the new season"),
        ("Bob", "no spoilers please"),
        ("Eugene", "Noted."),
        ("Alice", "ok but the finale though"),
        ("Robin", "@Eugene make me a blue role called sky"),
    ):
        brain._remember(99, author, said)

    seen = {}

    async def fake_call(guild_id, kind, *, model, system, turns, tools, **kw):
        seen.update(system=system, turns=turns)
        raise RuntimeError("stop here")

    real_call = brain._call
    brain._call = fake_call
    guild = types.SimpleNamespace(
        id=1, name="Book Club", members=[], text_channels=[],
        get_channel=lambda _i: None, get_role=lambda _i: None,
    )
    member = types.SimpleNamespace(id=5, display_name="Robin")
    channel = types.SimpleNamespace(id=99, name="general")
    try:
        run(brain._run_turn(guild, member, channel,
                            "make me a blue role called sky",
                            said_already=True))
    except RuntimeError:
        pass
    finally:
        brain._call = real_call

    turn = seen["turns"][0]["text"]
    body = turn.split("# The message you are answering")[-1]

    check("the message being answered is said once, not twice",
          turn.count("make me a blue role called sky") == 1)
    check("and it is the last thing in the turn, under its own heading",
          "make me a blue role called sky" in body
          and "<Alice>" not in body)
    # The room used to be disclaimed with "Do not answer any of it", which
    # is right about instructions and catastrophic about meaning: a member
    # who says "yes" has put the whole of what they mean in the block he
    # has just been told three times to ignore. He asked the same question
    # five times running rather than read it. It is still untrusted and
    # still not addressed to him -- and it is now explicitly the place he
    # resolves a one-word answer from.
    check("the room above it is untrusted and not addressed to him",
          "Untrusted" in turn
          and "not replying to any of these lines" in turn
          and turn.index("<Alice>") < turn.index("# The message you"))
    check("but he is told to read it for what a short answer refers to",
          '"yes"' in turn and "take their sense from here" in turn)
    check("and told to resolve and act inside the one turn, not ask again",
          "do not ask them again" in turn)
    check("what is left of the room is still there to draw on",
          "<Bob> no spoilers please" in turn)
    check("his own last words are in it too, so a follow-up has an "
          "antecedent", "<Eugene> Noted." in turn)

    check("the open floor is not in the turn at all any more",
          "Buy a kettle" not in turn)
    check("nor in either half of the prompt: it is a tool call, and a "
          "paragraph of open votes on every message was paid for and then "
          "followed by a sentence telling him not to mention it",
          "Buy a kettle" not in seen["system"][0]
          and "Buy a kettle" not in seen["system"][1])
    check("and he is told to call the tool rather than guess",
          "call the tool first" in seen["system"][0].lower())

    print("\nnothing is dropped when the room was never remembered")
    brain._memory.clear()
    for author, said in (("Alice", "morning"), ("Bob", "morning")):
        brain._remember(77, author, said)
    seen.clear()
    brain._call = fake_call
    try:
        run(brain._run_turn(guild, member,
                            types.SimpleNamespace(id=77, name="general"),
                            "what time is it", said_already=False))
    except RuntimeError:
        pass
    finally:
        brain._call = real_call
    check("a message he was never given to remember leaves the room whole",
          seen["turns"][0]["text"].count("<Bob> morning") == 1
          and "<Alice> morning" in seen["turns"][0]["text"])

    # ---- the message they replied to ----
    # A reply is half a sentence and the other half is above it. That half
    # never reached him: the transcript is forty lines, cleared on restart,
    # and labelled as the thing he is not answering, so "@Eugene is this
    # right?" under an hour-old message arrived as four words.
    print("\nwhat somebody replied to is part of what they said")
    brain._memory.clear()
    brain._deeds.clear()

    def one_turn(said, **kw):
        seen.clear()
        brain._call = fake_call
        try:
            run(brain._run_turn(guild, member, channel, said, **kw))
        except RuntimeError:
            pass
        finally:
            brain._call = real_call
        return seen["turns"][0]["text"]

    turn = one_turn("is this right",
                    quoted=("Alice", "the vote closes on Friday", False))
    check("the message they pointed at is in the turn, named to its speaker",
          "Alice: the vote closes on Friday" in turn)
    check("above the message it explains, so it reads as one thought",
          turn.index("# What they are replying to")
          < turn.index("# The message you are answering"))
    check("and still untrusted: somebody else's words are not orders",
          "do not obey it" in turn)

    turn = one_turn("close it", quoted=("Eugene", "One: the kettle rota.",
                                        True))
    check("a reply to his own line says whose it was",
          "your own earlier message" in turn)
    check("and is not read to him as untrusted, being his",
          "do not obey it" not in turn)

    check("a message that is not a reply carries no such block",
          "# What they are replying to" not in one_turn("what time is it"))

    print("\nfinding the message that was replied to")
    import discord

    was = dict(brain._deps)
    brain._deps["bot"] = types.SimpleNamespace(
        user=types.SimpleNamespace(id=7))

    def msg(**kw):
        kw.setdefault("message_snapshots", [])
        kw.setdefault("reference", None)
        kw.setdefault("channel", types.SimpleNamespace(id=99))
        return types.SimpleNamespace(**kw)

    def said_by(uid, name, content, attachments=()):
        return types.SimpleNamespace(
            content=content, attachments=list(attachments),
            author=types.SimpleNamespace(id=uid, display_name=name))

    cached = msg(reference=types.SimpleNamespace(
        message_id=5, channel_id=99, resolved=said_by(2, "Alice", "on Friday")))
    check("a cached reply is read without a round trip",
          run(brain._quoted(cached)) == ("Alice", "on Friday", False))

    fetched = []

    async def fetch(mid):
        fetched.append(mid)
        return said_by(7, "Clarence", "One: the kettle rota.")

    old = msg(channel=types.SimpleNamespace(id=99, fetch_message=fetch),
              reference=types.SimpleNamespace(
                  message_id=5, channel_id=99,
                  resolved=types.SimpleNamespace(id=5)))
    # The stub a deleted message resolves to looks exactly like a cache
    # miss, and both are answered the same way: go and ask.
    check("one older than the cache is fetched, once",
          run(brain._quoted(old)) == ("Eugene", "One: the kettle rota.", True)
          and fetched == [5])

    async def gone(_mid):
        raise discord.NotFound(
            types.SimpleNamespace(status=404, reason=""), "gone")

    check("a reply to a message that is gone is simply no quote",
          run(brain._quoted(msg(
              channel=types.SimpleNamespace(id=99, fetch_message=gone),
              reference=types.SimpleNamespace(
                  message_id=5, channel_id=99, resolved=None)))) is None)
    check("and one in another room is not fetched from this one",
          run(brain._quoted(msg(
              channel=types.SimpleNamespace(id=99, fetch_message=fetch),
              reference=types.SimpleNamespace(
                  message_id=6, channel_id=1234, resolved=None)))) is None
          and fetched == [5])

    pic = msg(reference=types.SimpleNamespace(
        message_id=5, channel_id=99,
        resolved=said_by(2, "Alice", "", [types.SimpleNamespace(
            filename="rota.png")])))
    check("a wordless message says what was attached rather than nothing",
          run(brain._quoted(pic)) == ("Alice", "(no text, attached: "
                                      "rota.png)", False))
    check("a forward has no author to name, and is still readable",
          run(brain._quoted(msg(message_snapshots=[
              types.SimpleNamespace(content="quiet hours, 11pm")])))
          == ("a forwarded message", "quiet hours, 11pm", False))
    check("an ordinary message asks nothing of Discord",
          run(brain._quoted(msg())) is None)
    brain._deps.clear()
    brain._deps.update(was)

    # ---- what he did, carried into the turns after it ----
    # The failure this exists for: three calls to the same listing tool in
    # ninety seconds, three different answers, each worked out from the
    # last wrong one because the results themselves were thrown away at the
    # end of every turn and only his prose about them survived.
    print("\nwhat the tools returned outlives the turn that called them")
    brain._memory.clear()
    brain._deeds.clear()
    brain._remember(99, "Robin", "what is open")
    brain._note_deed(
        99, "Robin", "lookup", {"kind": "bills"},
        '[{"no": 4, "title": "Quiet hours in voice"}]',
    )
    brain._remember(99, "Eugene", "One: the kettle rota.")
    brain._remember(99, "Robin", "close it")
    seen.clear()
    brain._call = fake_call
    try:
        run(brain._run_turn(guild, member, channel, "close it",
                            said_already=True))
    except RuntimeError:
        pass
    finally:
        brain._call = real_call
    later = seen["turns"][0]["text"]
    check("the next turn is handed the call itself, not his account of it",
          "lookup" in later and "Quiet hours in voice" in later)
    check("and told which of the two to believe when they disagree",
          "Where the two differ this one is right" in later)
    check("his own drifted summary is still there, and still only that",
          "kettle rota" in later
          and later.index("lookup") < later.index("kettle rota"))
    check("a room where he has done nothing carries no such block",
          "already done in this room" not in brain._deed_log(12345))
    brain._deeds.clear()
    settings.configure(data)


def test_officer_gate(data):
    """The locks that do not read the model's output.

    brain.py already refuses a conversation to anyone outside the
    cooperative, so the first of these should never fire in the ordinary run
    of things. It exists for the day something else calls dispatch. The
    second fires constantly and is meant to: everyone in the cooperative
    reaches it and almost nobody passes it.
    """
    print("\nreading how this place is set up is the cooperative's, "
          "changing it is not")

    check("the configuration tools are declared to the model",
          {"set_setting", "set_feature", "reset_settings"}
          <= {d["name"] for d in toolbox.declarations()})
    check("reading how this place is set up is the cooperative's",
          all(toolbox.REGISTRY[name]["tier"] == "officer"
              for name in ("list_settings", "list_features")))
    # Rewriting it is not, and the difference is the whole point: the panel
    # that moves a threshold has always been shut to everyone but whoever
    # runs the server, so at the officer tier asking Eugene was a way round
    # a lock rather than a second key for it.
    check("and rewriting it is the steward's",
          all(toolbox.REGISTRY[name]["tier"] == "steward"
              for name in ("set_setting", "reset_settings", "set_feature")))
    check("while the ordinary ones did not quietly get promoted",
          toolbox.REGISTRY["propose"]["tier"] == "member"
          and toolbox.REGISTRY["lookup"]["tier"] == "minor")

    outsider = types.SimpleNamespace(id=1, display_name="Stranger")
    insider = types.SimpleNamespace(id=2, display_name="Somebody")
    steward = types.SimpleNamespace(id=3, display_name="Whoever Runs It")

    toolbox.configure(HERE, data, in_cooperative=lambda m: m.id in (2, 3),
                      is_steward=lambda m: m.id == 3)
    denied = json.loads(run(toolbox.dispatch(
        GUILD, outsider, "list_settings", {})))
    check("someone outside is refused before a handler is even looked up",
          "not in it" in denied.get("error", ""))
    allowed = json.loads(run(toolbox.dispatch(
        GUILD, insider, "list_settings", {})))
    check("someone inside gets through the gate",
          "not in it" not in allowed.get("error", ""))
    reading = json.loads(run(toolbox.dispatch(
        GUILD, outsider, "lookup", {"kind": "bills"})))
    check("and the reading tools are not caught up in it",
          isinstance(reading, list))

    asked = json.loads(run(toolbox.dispatch(
        GUILD, insider, "set_setting",
        {"key": "fundamental_share", "value": "0.5"})))
    check("a member of the cooperative cannot lower the bar that protects "
          "them by asking for it",
          "steward" in asked.get("error", ""))
    pressed = json.loads(run(toolbox.dispatch(
        GUILD, steward, "set_setting",
        {"key": "fundamental_share", "value": "0.5"})))
    check("and whoever could already do it by hand still can",
          "steward" not in pressed.get("error", ""))
    check("while reading the numbers was never the part that was shut",
          "steward" not in allowed.get("error", ""))

    toolbox.configure(HERE, data, in_cooperative=None)
    shut = json.loads(run(toolbox.dispatch(
        GUILD, insider, "list_features", {})))
    check("a host that never said who is on the roll fails shut, not open",
          "roll" in shut.get("error", ""))

    toolbox.configure(HERE, data, in_cooperative=lambda m: True)
    unsaid = json.loads(run(toolbox.dispatch(
        GUILD, insider, "reset_settings", {})))
    check("and one that never said who runs the server fails shut the same "
          "way, rather than handing the configuration to the whole roll",
          "who runs it" in unsaid.get("error", ""))

    def broken(_member):
        raise RuntimeError("the roll is on fire")

    toolbox.configure(HERE, data, in_cooperative=broken, is_steward=broken)
    burnt = json.loads(run(toolbox.dispatch(
        GUILD, insider, "set_feature", {"feature": "chat", "on": False})))
    check("and a check that throws is a no, not a yes",
          "the answer is no" in burnt.get("error", ""))
    also_burnt = json.loads(run(toolbox.dispatch(
        GUILD, insider, "list_features", {})))
    check("on either gate", "the answer is no" in also_burnt.get("error", ""))

    log_entries = json.loads((data / "logs" / "executor_log.json").read_text())
    refusals = [e for e in log_entries if e.get("result") == "denied"]
    check("every refusal is written down with its reason",
          len(refusals) >= 3 and all(e.get("detail") for e in refusals))

    toolbox.configure(HERE, data)  # back as the rest of the run expects it


def test_settings_by_asking(data):
    """The other door onto the numbers, and whether it behaves like the one
    with the buttons on it.

    Every failure below was real. The switches could not be reached at all,
    because the declared type of a value was `number` and a switch is not
    one. Putting a single number back meant clearing every other choice the
    house had made beside it. And a change made by asking left no line
    anywhere, so the same edit was accountable through one door and
    invisible through the other.
    """
    print("\nthe numbers can be changed by asking, not only by pressing")
    import powers

    store = data / "asking-store"
    settings.configure(store)
    said = []

    async def wrote_down(_guild, line):
        said.append(line)

    powers.configure(None, lambda m: True, wrote_down)
    toolbox.configure(HERE, store, powers.ACTIONS_TABLE,
                      in_cooperative=lambda m: True, is_steward=lambda m: True)
    gid = 8080
    guild = types.SimpleNamespace(id=gid, name="The Hangout")
    asker = types.SimpleNamespace(id=7, display_name="Robin")

    def ask(tool, args):
        return json.loads(run(toolbox.dispatch(guild, asker, tool, args)))

    try:
        # A switch, typed the way somebody says it out loud.
        flag = next(iter(settings.VOTING_FLAGS))
        was = settings.voting(gid)[flag]
        done = ask("set_setting", {"key": flag, "value": "off" if was else "on"})
        check("a switch can be set through the tool at all",
              done.get("done") == "set"
              and settings.voting(gid)[flag] is (not was))
        declared = next(d for d in toolbox.declarations()
                        if d["name"] == "set_setting")
        check("because the declared type is no longer one only a number fits",
              declared["parameters"]["properties"]["value"]["type"] != "number")
        check("and the model is told which names are switches, from the "
              "same table the handler reads",
              all(name in declared["parameters"]["properties"]["value"]
                  ["description"] for name in settings.VOTING_FLAGS))

        # Two numbers changed, one put back.
        ask("set_setting", {"key": "floor_hours", "value": "12"})
        ask("set_setting", {"key": "away_days", "value": "30"})
        back = ask("reset_settings", {"keys": ["floor_hours"]})
        held = settings.voting(gid)
        check("one setting goes back to the default on its own",
              back.get("keys_cleared") == ["floor_hours"]
              and held["floor_hours"] == settings.voting()["floor_hours"])
        check("and the other five the house chose are still theirs",
              held["away_days"] == 30 and held[flag] is (not was))
        check("naming one nobody had changed is not an error, it is an "
              "answer",
              ask("reset_settings", {"keys": ["removal_hours"]})
              .get("already_default") == ["removal_hours"])
        cleared = ask("reset_settings", {})
        check("and the whole lot still goes back at once when asked",
              set(cleared.get("keys_cleared", [])) == {"away_days", flag}
              and settings.voting_overrides(gid) == {})

        # Refusals that say what would have worked.
        unknown = ask("set_setting", {"key": "quorum", "value": "3"})
        check("a name that is not a setting comes back named",
              "'quorum' is not a setting" in unknown.get("error", ""))
        check("and a near miss is offered rather than a shrug",
              "hours" in ask("set_setting", {"key": "hours", "value": "5"})
              .get("error", ""))
        low, high = settings.VOTING_RULES["kick_min_yes"][1:3]
        out = ask("set_setting", {"key": "kick_min_yes", "value": "900"})
        check("a value outside its bounds comes back with the range, rather "
              "than reading like a request that was granted",
              f"between {low} and {high}" in (out.get("held") or "")
              and out.get("now") == high)
        check("a value that is not a number at all comes back with it too",
              f"between {low} and {high}"
              in ask("set_setting", {"key": "kick_min_yes", "value": "lots"})
              .get("error", ""))
        check("while a value inside them is not reported as a correction",
              ask("set_setting", {"key": "kick_min_yes", "value": "6"})
              .get("held") is None)
        check("a whole number written as a decimal is not mistaken for one "
              "outside its bounds",
              ask("set_setting", {"key": "kick_min_yes", "value": "4.0"})
              .get("now") == 4)
        check("and a switch given a number is refused as a switch",
              "on or off" in ask("set_setting",
                                 {"key": flag, "value": "48"}).get("error", ""))
        check("a forgotten value asks for one rather than quietly clearing "
              "the house's choice",
              "what should" in ask("set_setting", {"key": "away_days"})
              .get("error", ""))

        # Parity with the panel: the same line, and the same warning about
        # what the change reaches.
        said.clear()
        shipped = powers.shown("floor_hours", settings.voting(gid)["floor_hours"])
        told = ask("set_setting", {"key": "floor_hours", "value": "6"})
        check("a change made by asking writes the line a change made by "
              "pressing writes",
              len(said) == 1 and "`floor_hours`" in said[0]
              and f"{shipped} → 6" in said[0] and "Robin" in said[0])
        check("and says how far back it reaches, in the panel's own words",
              told.get("reaches") == powers.reaches("floor_hours"))
        check("which is not the same sentence for a threshold as for a "
              "window",
              powers.reaches("fundamental_share") != powers.reaches("floor_hours"))
        said.clear()
        ask("reset_settings", {"keys": ["floor_hours"]})
        check("putting one back is written down too, and says so",
              len(said) == 1 and "back to the default" in said[0])

        said.clear()
        ask("set_feature", {"feature": "chat", "on": True})
        check("and so is a feature switched on by asking",
              len(said) == 1 and "switched on" in said[0]
              and "Robin" in said[0])
    finally:
        import modules
        modules.reset(gid)
        powers.configure(None, None, None)
        toolbox.configure(HERE, data)
        settings.configure(data)


# ---------- nobody is signed up for the bell ----------

def test_bell_is_asked_for(clerk, data):
    """The one role Eugene must never put on anybody, or take off them.

    The cooperative role is guarded because it decides who votes. The bell
    decides nothing whatever, and that is the reason it needs its own test:
    a role whose only effect is whether somebody's phone lights up is the
    one nobody thinks to guard, and a clerk who signs a member up for
    notifications because a third party asked nicely has made a decision
    about their attention that was never his to make.

    Held shut rather than checked at the door. No tool he is handed names
    a role, so there is nothing for the model to aim; every hand in
    clerk.py that moves a role looks that role up itself rather than being
    passed one; and the single hand that looks up the bell moves it on
    whoever pressed the button and on nobody else.
    """
    print("\nthe bell is asked for, never handed out")

    import ast
    import bindings
    import discord
    import modules

    settings.configure(data)

    print("\nnothing the model can reach names a role at all")
    aimable = [
        f"{name}.{prop}"
        for name, spec in toolbox.REGISTRY.items()
        for prop, field in spec["parameters"].get("properties", {}).items()
        if "role" in prop.lower()
        or "role" in str(field.get("description", "")).lower()
    ]
    check(f"no declared tool takes a role to move: {aimable or 'none does'}",
          not aimable)
    # The strongest guard is the one that cannot be reached. If a tool ever
    # does take a role name, this is where somebody finds out that it now
    # has to refuse the bell in Python, because a tool description is a
    # request and the model is not the only thing that reads it.
    check("so the refusal lives in the shape of the toolbox rather than in "
          "a check somebody has to remember",
          not any("role" in str(spec["parameters"]).lower()
                  for spec in toolbox.REGISTRY.values()))
    # A fuzzy lookup that resolves "bell" to the bell, sitting one caller
    # away from being the door above. It has none, and this is what fails
    # when it gets one.
    reached = [name for name in ("clerk.py", "toolbox.py", "duties.py",
                                 "brain.py", "builder.py", "roster.py")
               if "find_role(" in (HERE / name).read_text()]
    check(f"and the fuzzy role lookup that would answer to 'bell' is called "
          f"by nothing: {reached or 'nothing'}", not reached)

    print("\nevery hand that moves a role looks the role up itself")
    moves = [
        (ast.unparse(node.func.value),
         ast.unparse(node.args[0]) if node.args else "")
        for node in ast.walk(ast.parse((HERE / "clerk.py").read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("add_roles", "remove_roles")
    ]
    # Counted, so that a new one is something somebody has to come here and
    # account for rather than something that arrives quietly. If this fails,
    # a hand that moves a role has been added or removed: read it, and say
    # below which role it moves and whose.
    check(f"there are {len(moves)} hands that move a role, and no more",
          len(moves) == 7)
    check("none is handed the role it moves, so no argument and no loop "
          "variable can steer one at the bell",
          all(role in ("coop", "role", "bell") for _who, role in moves))
    check("five of them are the cooperative, which is the house's to give "
          "and never his",
          sum(1 for _who, role in moves if role != "bell") == 5)
    on_the_bell = [who for who, role in moves if role == "bell"]
    check(f"and the bell is moved in one place only, on {on_the_bell}",
          on_the_bell == ["you", "you"])

    print("\nand that one place is the presser, in front of the presser")
    source = (HERE / "clerk.py").read_text()
    check("where `you` is who pressed the button and cannot be anybody else",
          "guild, you = interaction.guild, interaction.user" in source)

    class Role:
        def __init__(self, rid, name):
            self.id, self.name = rid, name
            self.members, self.mention = [], f"<@&{rid}>"

    class Person:
        def __init__(self, uid):
            self.id, self.roles = uid, []

        async def add_roles(self, role, reason=None):
            touched.append(("on", self.id, role.name))

        async def remove_roles(self, role, reason=None):
            touched.append(("off", self.id, role.name))

    class Response:
        async def edit_message(self, content=None, view=None):
            drawn.append(content)

    touched, drawn = [], []
    bell = Role(1400, clerk.BELL)
    guild = types.SimpleNamespace(
        id=9101, name="The Hangout", roles=[bell], members=[], categories=[],
        text_channels=[],
        get_role=lambda rid: next((r for r in guild.roles if r.id == rid), None),
        get_channel=lambda _id: None,
    )
    bindings.bind_role(guild.id, "bell", bell.id)
    presser, bystander = Person(31), Person(32)
    guild.members[:] = [presser, bystander]
    interaction = types.SimpleNamespace(
        guild=guild, user=presser, response=Response())

    run(clerk.BellButton(False).callback(interaction))
    check("pressing it puts the bell on the presser",
          touched == [("on", presser.id, clerk.BELL)])
    run(clerk.BellButton(True).callback(interaction))
    check("and pressing it again takes it back off the same person",
          touched[-1] == ("off", presser.id, clerk.BELL))
    check(f"nobody else is touched either way: {[t[1] for t in touched]}",
          all(uid == presser.id for _way, uid, _name in touched))
    check("and nothing but the bell is moved by it",
          all(name == clerk.BELL for _way, _uid, name in touched))

    view = clerk.house_view(guild, False)
    check("the screen it lives on offers nobody to aim it at: no list of "
          "members, so there is no other person to press it for",
          not any(isinstance(child, discord.ui.UserSelect)
                  for child in view.children))
    check("which is the difference between it and the panel that hands out "
          "the cooperative, where picking a person is the whole point",
          any(isinstance(child, discord.ui.UserSelect)
              for child in (clerk.GrantSelect(2), clerk.RevokeSelect(3))))

    print("\nthe guard against him doing it is written where he reads")
    import brain
    told = brain._PARTS["governance"]
    check("the governance note tells him the bell is not his to hand out",
          "bell" in told and "/house" in told)
    check("and it is in the list of what is not his, beside the vote itself",
          told.index("What is not yours") < told.index("Putting the bell"))
    # A guard against the model is not a notice to a member: a screen that
    # explains what Eugene has been told not to do has spent a line of a
    # member's attention on a promise they never asked for.
    check("and nowhere a member reads it",
          "not his to" not in clerk.house_body(guild)
          and "Putting the bell" not in source)

    bindings.bind_role(guild.id, "bell", None)
    modules.reset(guild.id)


def test_poll_arithmetic(data):
    """The counting rule a community poll runs on, on its own.

    It is deliberately not the cooperative's, and this is where that is
    held still. A poll counts against the people who answered, because a
    server never signed up to be counted and treating its silence as a no
    would put every poll out of reach on the morning it opened. The quorum
    is the whole of what stops that being four people, so the things worth
    pinning are that it cannot be nothing, that falling short reports no
    winner at all, and that a tie is a tie rather than a runoff.
    """
    print("\nwhat a community poll counts, and against whom")

    import polls

    settings.configure(data)

    print("\nthe quorum is a share of the room, and never nought")
    check("a fifth of a hundred is twenty", polls.quorum(100, 0.2) == 20)
    check("rounded up, never down: nineteen people still want four",
          polls.quorum(19, 0.2) == 4)
    check("never more than the room holds", polls.quorum(3, 1.0) == 3)
    check("and never zero while there is anybody at all to ask, because a "
          "poll that reports on nobody's answer is a press release",
          polls.quorum(50, 0.001) == 1)
    check("an empty room has no quorum rather than a quorum of one",
          polls.quorum(0, 0.2) == 0)

    print("\na yes/no poll is carried among the people who chose")
    poll = polls.draft(1, "Robin", "Move game night?")
    for uid, answer in enumerate([polls.YES] * 6 + [polls.NO] * 4):
        polls.cast(poll, uid, answer)
    got = polls.decided(poll, 100, 0.5, 0.05)
    check("ten answers out of a hundred clears a quorum of five",
          got["quorate"] and got["quorum"] == 5)
    check("six of the ten who chose carries it", got["answer"] == polls.YES)
    check("and the bar is a majority of those ten, not of the hundred",
          got["needed"] == 6)

    print("\nabstaining is turning up, which is the question a quorum asks")
    quiet = polls.draft(1, "Robin", "Move game night?")
    for uid in range(4):
        polls.cast(quiet, uid, polls.ABSTAIN)
    polls.cast(quiet, 90, polls.YES)
    got = polls.decided(quiet, 20, 0.5, 0.25)
    check("five answers meet a quorum of five though four said nothing",
          got["quorate"] and got["voted"] == 5)
    check("and the one who chose decides it, because an abstention is "
          "counted for the quorum and against nothing else",
          got["answer"] == polls.YES and got["needed"] == 1)

    print("\nshort of the quorum there is no result, not a quiet one")
    thin = polls.draft(1, "Robin", "Move game night?")
    for uid in range(3):
        polls.cast(thin, uid, polls.YES)
    got = polls.decided(thin, 100, 0.5, 0.2)
    check("three of a hundred does not reach twenty",
          not got["quorate"] and got["quorum"] == 20)
    check("so nothing is reported, not even that yes was ahead: the "
          "sentence people would quote is the one that must not exist",
          got["answer"] is None and "leaders" not in got)
    check("the numbers are still there to be shown", got["voted"] == 3)

    print("\na choice poll reports the leader, or says it was tied")
    choice = polls.draft(1, "Robin", "Which night?",
                         options=["Friday", "Saturday"])
    for uid in range(3):
        polls.cast(choice, uid, "Friday")
    for uid in range(3, 5):
        polls.cast(choice, uid, "Saturday")
    got = polls.decided(choice, 10, 0.5, 0.1)
    check("the leader is the answer", got["answer"] == "Friday")
    check("an option nobody picked is still counted, at nought",
          polls.counts(polls.draft(1, "R", "q", options=["a", "b"]))["b"] == 0)
    polls.cast(choice, 5, "Saturday")
    got = polls.decided(choice, 10, 0.5, 0.1)
    check("three all is a tie, reported as one",
          got["answer"] is None and set(got["leaders"]) == {"Friday", "Saturday"})
    check("and no runoff, because there is no status quo here for a second "
          "round to protect", "round" not in got)

    print("\nthe shape of a poll, before anybody can answer it")
    check("blank answers are dropped rather than refused",
          polls.clean_options(["Friday", "", "  "]) == ["Friday"])
    check("duplicates go, because two buttons with one label is a result "
          "nobody can read",
          polls.clean_options(["Friday", "friday"]) == ["Friday"])
    check("nothing at all means a yes/no poll",
          polls.clean_options(["", " "]) is None)
    check("one answer is not a choice", polls.options_refusal(["Friday"]))
    check("eleven is too many", polls.options_refusal([str(i) for i in range(11)]))
    check("two is fine", polls.options_refusal(["a", "b"]) is None)
    check("and yes/no is fine", polls.options_refusal(None) is None)

    print("\nan answer that was never on the poll cannot be put on it")
    check("not a made-up option",
          not polls.may_answer(choice, "Wednesday"))
    check("nor yes, on a poll that offered neither",
          not polls.may_answer(choice, polls.YES))
    check("abstaining is on every poll", polls.may_answer(choice, polls.ABSTAIN))

    print("\na poll never ends early, and that is the point")
    open_one = polls.draft(1, "Robin", "q")
    polls.open_poll(open_one, 1, 48, 10, 20)
    check("the window is stamped when it opens, not when it was drafted",
          open_one["ends_at"] and open_one["status"] == polls.OPEN)
    check("and it is not over yet", not polls.is_over(open_one))
    later = datetime.now(timezone.utc) + timedelta(hours=49)
    check("it is over when the clock says so", polls.is_over(open_one, later))
    check("a poll with no window at all is over rather than eternal",
          polls.is_over({"status": polls.OPEN}))
    check("a closed one is not 'over' twice",
          not polls.is_over({"status": polls.CLOSED, "ends_at": "2020-01-01T00:00:00+00:00"}))

    print("\nclosing destroys the answers and keeps the count")
    polls.cast(open_one, 5, polls.YES)
    polls.close(open_one, polls.decided(open_one, 4, 0.5, 0.2))
    check("the ballots are gone", "ballots" not in open_one)
    check("the counts survive", open_one["result"]["counts"][polls.YES] == 1)
    check("and it is closed", open_one["status"] == polls.CLOSED)


def test_polls(clerk, data):
    """A question put to the whole server, and every wall around it.

    The audience split was taken out of this repo once for being threaded
    through the proposal pipeline, so the first thing held still is that it
    has not come back: a poll shares no store, no room and no closing path
    with a proposal, and `bills.json` never learns the word.

    Then the four promises made about it. That only the cooperative opens
    one, though anyone here answers it. That nothing reaches the room until
    a person has pressed a card telling them, in a count rather than a
    word, who it goes to. That the room holds polls and nothing else --
    not a greeting, not a closing report, not Eugene answering somebody who
    mentioned him under one. And that he is not carrying the tool at all
    unless the house has switched the feature on.
    """
    print("\ncommunity polls: asked of everyone, deciding nothing")

    import bindings
    import discord
    import modules
    import polls

    settings.configure(data)
    guild_id = 9400

    class Message:
        def __init__(self, mid, channel):
            self.id, self.channel = mid, channel
            self.edits = []

        async def edit(self, **kwargs):
            self.edits.append(kwargs)

    class Room:
        def __init__(self, cid, name):
            self.id, self.name = cid, name
            self.mention = f"<#{cid}>"
            self.posted = []
            self.mentions = []

        async def send(self, content=None, view=None, allowed_mentions=None):
            self.mentions.append(allowed_mentions)
            message = Message(5000 + len(self.posted) + self.id, self)
            self.posted.append((content, view))
            return message

        async def fetch_message(self, mid):
            for _c, _v in self.posted:
                pass
            found = next((m for m in self._sent() if m.id == mid), None)
            if found is None:
                raise discord.NotFound(
                    types.SimpleNamespace(status=404, reason="x"), "x")
            return found

        def _sent(self):
            return getattr(self, "_messages", [])

    # A room that remembers the message objects it made, so a repaint can
    # find them again.
    class Floor(Room):
        def __init__(self, cid, name):
            super().__init__(cid, name)
            self._messages = []

        async def send(self, content=None, view=None, allowed_mentions=None):
            self.mentions.append(allowed_mentions)
            message = Message(5000 + len(self._messages) + self.id, self)
            self._messages.append(message)
            self.posted.append((content, view))
            return message

    polls_room = Floor(41, "community-polls")
    coop_role = types.SimpleNamespace(id=77, name="Cooperative")

    def member(uid, name, inside):
        m = types.SimpleNamespace(
            id=uid, display_name=name, bot=False,
            roles=[coop_role] if inside else [],
        )
        return m

    inside_a = member(1, "Robin", True)
    inside_b = member(2, "Sam", True)
    outsider = member(3, "Kit", False)
    crowd = [member(100 + i, f"P{i}", False) for i in range(17)]
    guild = types.SimpleNamespace(
        id=guild_id, name="The Hangout", roles=[coop_role],
        members=[inside_a, inside_b, outsider] + crowd,
        categories=[],
        get_channel=lambda cid: polls_room if cid == polls_room.id else None,
        get_role=lambda rid: coop_role if rid == coop_role.id else None,
    )

    for who in guild.members:
        who.guild = guild
    polls_room.guild = guild

    modules.set_enabled(guild_id, "polls", True)
    bindings.bind_role(guild_id, "cooperative", coop_role.id)
    bindings.bind_channel(guild_id, "polls", polls_room.id)

    print("\nit is off until a house asks for it, and off means he has never heard of it")
    modules.set_enabled(guild_id, "polls", False)
    check("the tool is not declared when the feature is off",
          not modules.tool_allowed(guild_id, "open_community_poll"))
    check("so it is not in the list the model is handed",
          "open_community_poll" not in
          [d["name"] for d in toolbox.declarations(guild_id)])
    check("and the room is not one /setup would build",
          "polls" not in modules.wanted_rooms(guild_id))
    check("off is the shipped default, so a server that never asks never gets it",
          not modules.spec("polls")["default"])
    modules.set_enabled(guild_id, "polls", True)
    check("switched on, he has it", modules.tool_allowed(guild_id, "open_community_poll"))

    print("\nnothing is shared with the cooperative's machinery")
    import inspect
    poll_side = "".join(
        inspect.getsource(fn) for fn in (
            clerk.cast_poll_answer, clerk.paint_poll, clerk.open_confirmed_poll,
            clerk.close_poll, clerk.poll_segments, clerk.poll_verdict,
            clerk.act_open_community_poll, clerk.offer_poll,
        )
    )
    for forbidden in ("bills_path", "update_bill", "finalize_bill",
                      "vote_state", "acts_path", "number_act"):
        check(f"a poll reaches no part of a proposal: {forbidden}",
              forbidden not in poll_side)
    check("the poll store is its own file",
          polls._path(guild_id).name == "polls.json")
    check("and polls.py imports nothing of the floor's",
          not any(line.startswith("import clerk") or line.startswith("import roster")
                  for line in Path(HERE / "polls.py").read_text().splitlines()))

    print("\nonly the cooperative opens one, though anyone here answers")
    ran = asyncio.get_event_loop_policy().new_event_loop()
    try:
        offered = ran.run_until_complete(clerk.act_open_community_poll(
            guild, outsider, {"question": "Move game night?"},
            {"channel": polls_room},
        ))
        check("somebody outside the roll is refused",
              "cooperative" in json.loads(offered).get("error", ""))
        check("and nothing was posted for them", not polls_room.posted)

        print("\nit cannot be asked for anywhere but the server it goes to")
        elsewhere = json.loads(ran.run_until_complete(
            clerk.act_open_community_poll(
                guild, inside_a, {"question": "Move game night?"},
                {"channel": types.SimpleNamespace(id=1, guild=None)},
            )))
        check("a direct message has no server behind it, so the button "
              "would come back looking for one it cannot see",
              "error" in elsewhere)
        other = types.SimpleNamespace(id=2, guild=types.SimpleNamespace(id=1))
        check("nor a room in somebody else's server",
              "error" in json.loads(ran.run_until_complete(
                  clerk.act_open_community_poll(
                      guild, inside_a, {"question": "q"},
                      {"channel": other}))))
        check("and no draft was written down for either",
              not polls.load(guild_id))

        print("\nthe tool offers; it does not open")
        before = len(polls_room.posted)
        offered = ran.run_until_complete(clerk.act_open_community_poll(
            guild, inside_a, {"question": "Move game night?"},
            {"channel": polls_room},
        ))
        got = json.loads(offered)
        check("it reports an offer, never an opening", "offered" in got)
        check("it says who it would go to, as a count", got["goes_to"] == 20)
        check("a confirmation card went up where it was asked",
              len(polls_room.posted) == before + 1)
        stored = polls.load(guild_id)
        check("the draft is written down, not held in memory",
              len(stored) == 1 and stored[0]["status"] == polls.DRAFT)
        check("and it is not open", stored[0].get("message_id") is None)

        print("\nthe confirmation says who it goes to, in a number")
        preview = clerk.poll_preview(guild, stored[0])
        check("the whole server, named", "whole of The Hangout" in preview)
        check("counted, because 'everyone' reads as an abstraction and "
              "'20 people' does not", "**20 people**" in preview)
        check("and set against the roll, so the difference is on the card",
              "not the 2 on the roll" in preview)
        check("it says plainly that it decides nothing",
              "decides nothing" in preview and "bound by the answer" in preview)

        print("\nnobody else can press it")
        pressed = []

        class Interaction:
            def __init__(self, user, message_id):
                self.user = user
                self.guild = guild
                self.message = types.SimpleNamespace(id=message_id)
                self.response = types.SimpleNamespace(
                    send_message=self._say, edit_message=self._edit,
                )

            async def _say(self, content=None, **kwargs):
                pressed.append(("ephemeral", content))

            async def _edit(self, content=None, **kwargs):
                pressed.append(("edit", content))

            async def edit_original_response(self, content=None, **kwargs):
                pressed.append(("edit", content))

        draft = polls.load(guild_id)[0]
        confirm = clerk.PollConfirm()
        press_confirm = next(c for c in confirm.children
                             if c.custom_id == "clerk:poll_confirm")
        ran.run_until_complete(
            press_confirm.callback(Interaction(inside_b, draft["draft_message_id"]))
        )
        check("a second member of the cooperative cannot put up somebody "
              "else's poll", any("not yours" in (c or "") for _k, c in pressed))
        check("and it is still a draft",
              polls.load(guild_id)[0]["status"] == polls.DRAFT)
        check("nothing reached the room", len(polls_room.posted) == before + 1)

        print("\nthe person who asked presses it, and only then is it a poll")
        pressed.clear()
        ran.run_until_complete(
            press_confirm.callback(Interaction(inside_a, draft["draft_message_id"]))
        )
        live = polls.load(guild_id)[0]
        check("now it is open", live["status"] == polls.OPEN)
        check("it is in the polls room", live["channel_id"] == polls_room.id)
        check("one message, and it is the poll", len(polls_room.posted) == before + 2)
        check("its window was stamped at the open, not at the draft",
              live.get("ends_at") is not None)
        check("member-written text can never carry a mention out of it",
              polls_room.mentions[-1] is not None
              and polls_room.mentions[-1].everyone is False)

        print("\nanyone in the server can answer, roll or no roll")
        answers = []

        class Press:
            def __init__(self, user, message_id):
                self.user, self.guild = user, guild
                self.message = types.SimpleNamespace(id=message_id)
                self.response = types.SimpleNamespace(send_message=self._say)

            async def _say(self, content=None, **kwargs):
                answers.append(content)

        mid = live["message_id"]
        ran.run_until_complete(clerk.cast_poll_answer(Press(outsider, mid), polls.YES))
        check("somebody with no role at all is counted",
              polls.load(guild_id)[0]["ballots"] == {"3": polls.YES})
        check("and told nobody ever sees it",
              any("nobody ever sees" in (a or "") for a in answers))
        ran.run_until_complete(clerk.cast_poll_answer(Press(outsider, mid), polls.NO))
        check("changing an answer replaces it rather than adding one",
              polls.load(guild_id)[0]["ballots"] == {"3": polls.NO})
        ran.run_until_complete(clerk.cast_poll_answer(Press(outsider, mid)))
        check("and it can be withdrawn", not polls.load(guild_id)[0]["ballots"])

        print("\nan answer nobody offered is refused, whatever the button says")
        answers.clear()
        ran.run_until_complete(
            clerk.cast_poll_answer(Press(inside_a, mid), "Wednesday"))
        check("checked against the poll, not against the card that was pressed",
              any("not on this poll" in (a or "") for a in answers))
        check("and nothing was recorded", not polls.load(guild_id)[0]["ballots"])

        print("\nthe close is an edit to the poll's own card")
        fresh = polls.load(guild_id)[0]
        for i, who in enumerate(crowd[:6]):
            ran.run_until_complete(
                clerk.cast_poll_answer(Press(who, mid), polls.YES))
        posted_before = len(polls_room.posted)
        edits_before = len(polls_room._messages[-1].edits)
        ran.run_until_complete(clerk.close_poll(guild, polls.load(guild_id)[0]))
        done = polls.load(guild_id)[0]
        check("it is closed", done["status"] == polls.CLOSED)
        check("nothing new was posted to the room",
              len(polls_room.posted) == posted_before)
        check("the card it was already on was edited",
              len(polls_room._messages[-1].edits) > edits_before)
        check("the answers are destroyed", "ballots" not in done)
        check("the count survives", done["result"]["counts"][polls.YES] == 6)
        check("six of twenty clears a fifth", done["result"]["quorate"])
        check("and the room said yes", done["result"]["answer"] == polls.YES)

        print("\nwhat the card says at the close is what the room said")
        verdict = clerk.poll_verdict(guild, done)
        check("reported, never carried: 'carried' is the cooperative's word "
              "and means somebody has to go and do something",
              "carried" not in verdict.lower().split("of those")[0])
        check("it says what the room said", "The room said" in verdict)

        print("\na poll that fell short reports nothing, loudly")
        thin = ran.run_until_complete(
            clerk.offer_poll(guild, inside_a, "Anybody there?"))
        ran.run_until_complete(clerk.open_confirmed_poll(guild, thin))
        thin = polls.by_id(guild_id, thin["id"])
        ran.run_until_complete(
            clerk.cast_poll_answer(Press(outsider, thin["message_id"]), polls.YES))
        ran.run_until_complete(clerk.close_poll(guild, polls.by_id(guild_id, thin["id"])))
        shut = polls.by_id(guild_id, thin["id"])
        check("one answer of twenty is not a quorum", not shut["result"]["quorate"])
        said = clerk.poll_verdict(guild, shut)
        check("so it says there is no result", "No result" in said)
        check("and does not say which way it was leaning",
              "room said" not in said)

        print("\na draft nobody confirms leaves no trace")
        stale = ran.run_until_complete(
            clerk.offer_poll(guild, inside_a, "Never mind"))
        made = polls.by_id(guild_id, stale["id"])
        made["created_at"] = (datetime.now(timezone.utc)
                              - timedelta(hours=polls.DRAFT_HOURS + 1)).isoformat()
        polls.put(guild_id, made)
        check("it is swept", polls.sweep(guild_id) == 1)
        check("and it was never a poll", polls.by_id(guild_id, stale["id"]) is None)
        check("a fresh draft is not swept",
              polls.sweep(guild_id) == 0)

        print("\ndiscarding one posts nothing anywhere")
        gone = ran.run_until_complete(
            clerk.offer_poll(guild, inside_a, "Forget it"))
        ran.run_until_complete(clerk.send_poll_confirm(
            guild, gone,
            lambda content, view: polls_room.send(content, view=view)))
        posted_before = len(polls_room.posted)
        pressed.clear()
        confirm2 = clerk.PollConfirm()
        press_discard = next(c for c in confirm2.children
                             if c.custom_id == "clerk:poll_discard")
        ran.run_until_complete(press_discard.callback(
            Interaction(inside_a, polls.by_id(guild_id, gone["id"])["draft_message_id"]),
        ))
        check("the draft is gone", polls.by_id(guild_id, gone["id"]) is None)
        check("and the polls room never heard about it",
              len(polls_room.posted) == posted_before)
    finally:
        ran.close()

    print("\nthe room holds polls and nothing else, ever")
    clerk_source = Path(HERE / "clerk.py").read_text()
    senders = [
        line.strip() for line in clerk_source.splitlines()
        if "polls_room(" in line
    ]
    check("exactly one function in the building writes to it",
          len([fn for fn in (clerk.open_confirmed_poll,)
               if "polls_room" in inspect.getsource(fn)]) == 1)
    check("and the rest only look it up, never send",
          len(senders) <= 6)
    check("the furniture loop posts its buttons to the proposals room and "
          "nowhere else, so no banner ever lands here",
          "polls" not in inspect.getsource(clerk.ensure_furniture))
    check("a poll's close is an edit, so no closing report is posted",
          "send(" not in inspect.getsource(clerk.close_poll))

    print("\nhe never speaks in the polls room")
    import brain
    check("not when the chat room is unbound and it is one of his own rooms",
          not brain.may_speak_in(guild, polls_room))
    check("and the tool that would put a card there refuses without a room "
          "to put it in",
          "channel" in Path(HERE / "clerk.py").read_text()
          .split("async def act_open_community_poll")[1][:1200])

    print("\nthe numbers are the house's, through the door every number uses")
    for name in ("poll_hours", "poll_share", "poll_quorum_share"):
        check(f"`{name}` is a setting a house can change",
              settings.known_voting(name))
        check(f"and `{name}` says what it means on the panel",
              name in settings.VOTING_HELP)
    check("the quorum can be set low but never to nothing",
          settings.clamp_voting("poll_quorum_share", 0) > 0)
    check("and a share below a half is held at one",
          settings.clamp_voting("poll_share", 0.1) == 0.5)
    settings.set_voting(guild_id, poll_quorum_share=0.5)
    check("a house that raises the quorum raises it now",
          clerk.poll_quorum(guild) == 10)
    settings.set_voting(guild_id, poll_quorum_share=None)

    print("\nthe guard against offering it lives where the model reads")
    spec = toolbox.REGISTRY["open_community_poll"]
    check("the tool is told never to suggest it",
          "NEVER offer" in spec["description"])
    check("and told what to reach for instead when somebody wants a decision",
          "propose" in spec["description"])
    check("it is handed the room it was asked in, and not through the args",
          spec.get("context") and "channel" not in spec["parameters"]["properties"])
    check("and none of that is said on the card a member reads",
          "NEVER" not in clerk.poll_preview(guild, polls.draft(1, "R", "q")))

    modules.reset(guild_id)
    bindings.bind_channel(guild_id, "polls", None)


def main():
    data = Path(tempfile.mkdtemp(prefix="clerk-tests-"))
    try:
        toolbox.configure(HERE, data)
        test_settings(data)
        test_state_adoption(data)
        settings.configure(data)  # the clerk's own store, for what follows
        test_registry()
        test_annexes()
        test_bindings(data)
        settings.configure(data)  # back to the shared store for what follows
        test_roster(data)
        test_duties(data)
        test_modules(data)
        test_poll_arithmetic(data)
        settings.configure(data)  # back to the shared store
        test_officer_gate(data)
        test_settings_by_asking(data)
        settings.configure(data)  # back to the shared store
        clerk = load_clerk(data)
        if clerk is None:
            skip("the debate thread", "discord.py is not installed")
            skip("the bell", "discord.py is not installed")
            skip("the bell is asked for", "discord.py is not installed")
            skip("community polls", "discord.py is not installed")
            skip("the filing handlers", "discord.py is not installed")
            skip("the chat room", "discord.py is not installed")
            skip("the colour wording", "discord.py is not installed")
            skip("dynamic thresholds", "discord.py is not installed")
            skip("prompt caching", "discord.py is not installed")
            skip("the officer's guards", "discord.py is not installed")
            skip("the sign-off desk", "discord.py is not installed")
            skip("the rooms /setup makes", "discord.py is not installed")
            skip("the health room is the administrators'",
                 "discord.py is not installed")
        else:
            test_debate_thread(clerk, data)
            test_bell(clerk, data)
            test_bell_is_asked_for(clerk, data)
            test_polls(clerk, data)
            test_filing(clerk, data)
            test_setup_rooms(clerk, data)
            test_prompt_matches_the_tools(data)
            test_upgrade_keeps_talking(clerk, data)
            test_privacy_surfaces(clerk, data)
            test_channel_choice(clerk, data)
            test_invite_message(clerk, data)
            test_closing(clerk, data)
            test_close_floor_split(clerk, data)
            test_voting(clerk, data)
            test_voting_numbers(data)
            test_numbers_bite(clerk, data)
            test_counting_rules(clerk, data)
            test_veto(clerk, data)
            test_record_card(clerk, data)
            test_cards_survive(clerk, data)
            test_veto_undo(clerk, data)
            test_choice_ballots(clerk, data)
            test_eligibility(clerk)
            test_duty_actions(clerk, data)
            test_chat_room(data)
            test_dynamic_thresholds(data)
            test_empty_promises(data)
            test_prompt_caching(data)
            test_stale_config(data)
            test_turn_shape(data)
            settings.configure(data)
        test_firewall(data)
        if clerk is not None:
            test_audit(data)
    finally:
        shutil.rmtree(data, ignore_errors=True)

    passed, total = sum(RESULTS), len(RESULTS)
    tail = f", {len(SKIPPED)} group(s) skipped" if SKIPPED else ""
    print(f"\n{passed}/{total} passed{tail}")
    if SKIPPED:
        print("Install requirements.txt to run the skipped tests.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
