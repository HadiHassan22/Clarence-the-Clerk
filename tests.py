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
CITIZEN = types.SimpleNamespace(id=99, display_name="Hadi")


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

    settings.set_brain_key(one, "gemini", "AIzaSecretOne", by="Hadi")
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
    for name in ("propose_bill", "propose_member"):
        check(f"{name} is declared to the model", name in names)
        check(f"{name} sits in the member tier",
              toolbox.REGISTRY[name]["tier"] == "member")
        check(f"{name} takes its handler from clerk.py",
              toolbox.REGISTRY[name]["handler"] is None)
    member_spec = next(d for d in toolbox.declarations() if d["name"] == "propose_member")
    check("propose_member requires a name and a why",
          set(member_spec["parameters"]["required"]) == {"name", "why"})
    bill_spec = next(d for d in toolbox.declarations() if d["name"] == "propose_bill")
    check("propose_bill requires all three fields",
          set(bill_spec["parameters"]["required"]) == {"title", "what", "why"})

    # The colour caps were typed into these descriptions by hand -- "five at
    # most" -- while the shipped cap was one. A single request carried both
    # numbers, the true one in the system prompt and the false one here,
    # and the model believed the tool: it offered a member a second colour
    # and was refused by its own harness in front of them. The descriptions
    # quote the house now, from the same place every other reader reads.
    def described(name, guild_id=None):
        return next(d for d in toolbox.declarations(guild_id)
                    if d["name"] == name)["description"]

    stored = (toolbox.REGISTRY["create_color_role"]["description"]
              + toolbox.REGISTRY["wear_color_role"]["description"])
    check("no colour tool stores a limit typed in by hand",
          "five" not in stored and "{make}" in stored and "{wear}" in stored)
    check("the limit a tool quotes is the one the house enforces",
          f"make {settings.voting()['role_create_max']} of their own"
          in described("create_color_role"))
    settings.configure_voting(role_create_max=4, role_wear_max=3)
    check("a house that moves a cap has moved what the tool says it is",
          "make 4 of their own" in described("create_color_role")
          and "3 at a time" in described("wear_color_role"))
    settings.configure_voting(role_create_max=1, role_wear_max=5)
    check("and a description with no figure in it is passed through whole",
          "{" not in described("propose_bill"))


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
    (data / "bills.json").write_text(json.dumps([{
        "no": 1, "title": "t", "kind": "invite", "status": "passed",
        "author": "x", "ballots": {"111": "yes", "222": "abstain"},
        "tally": {"yes": 5, "no": 1, "abstain": 2}, "tally_line": "5/1/2",
        "invite_url": "https://discord.gg/secret", "notes": {}}]))
    bill = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "get_bill", {"bill_no": 1})))
    check("individual ballots are stripped", "ballots" not in bill)
    check("the tally is sealed, abstentions included",
          "tally" not in bill and "tally_line" not in bill)
    check("the invite link is never handed to the model", "invite_url" not in bill)


def test_audit(data):
    print("\nevery dispatch is audited")
    check("the audit log is written under logs/, not in among the record",
          (data / "logs" / "executor_log.json").exists()
          and not (data / "executor_log.json").exists())
    entries = json.loads((data / "logs" / "executor_log.json").read_text())
    check(f"{len(entries)} entries written", len(entries) >= 10)
    check("proposals are logged with their arguments",
          any(e["tool"] == "propose_member" and e["args"].get("name") for e in entries))
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

        async def create_thread(self, name=None, auto_archive_duration=None,
                                **kwargs):
            notes = Thread(200, name)
            notes.window = auto_archive_duration
            threads.append(notes)
            return notes

    class Floor:
        id, mention, said = 500, "<#500>", 0

        async def send(self, content=None, view=None):
            Floor.said += 1
            return Message(600 + Floor.said)

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
    author = types.SimpleNamespace(id=7, display_name="Hadi", mention="@Hadi")

    keep_floor, keep_content = clerk.floor_for, clerk.ballot_content
    clerk.floor_for = lambda _guild, _bill: floor
    clerk.ballot_content = lambda _guild, _bill: "the ballot"
    try:
        bill = run(clerk.file_bill(
            guild, author, "A books channel",
            "There shall be a books channel.", "People here read."))
    finally:
        clerk.floor_for, clerk.ballot_content = keep_floor, keep_content

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


# ---------- the filing handlers, for real ----------

def test_filing(clerk, data):
    filed = {}
    open_bills = []

    def set_floor(bills):
        open_bills[:] = bills
        clerk.save_json(clerk.BILLS, bills)

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

    print("\npropose_member")
    set_floor([])
    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "propose_member", {
        "name": "Sam", "discord_id": "123456789012345678",
        "why": "They have been in the group chat for a year."})))
    check(f"the bill is filed: No. {out.get('filed')}", out.get("filed") == 13)
    check("it is filed as an invite bill", filed.get("kind") == "invite")
    check("the citizen who asked is the author, not the clerk",
          out.get("author") == "Hadi")
    check("the ballot is advertised as three-way and sealed",
          "abstain" in out.get("ballot", "") and "sealed" in out.get("ballot", ""))
    check("the text names the person and the consequence",
          "Sam" in filed.get("what", "")
          and "single-use invite link" in filed.get("what", ""))
    check("a supplied Discord ID is carried into the bill",
          "123456789012345678" in filed.get("what", ""))
    check("their reasons are preserved verbatim",
          "in the group chat for a year" in filed.get("why", ""))
    # Two doors, and they were read as one for a while: an invitation is
    # somebody let into the server, never somebody handed a vote. The bill
    # is what a voter reads before deciding, so it has to say which.
    check("the bill says outright that it is not a seat in the cooperative",
          "not a place in the cooperative" in filed.get("what", ""))
    check("and the tool that files it says the same to the model",
          "does not put anyone in the cooperative"
          in toolbox.BILL_TOOLS["propose_member"]["description"])
    check("and somebody outside is sent to the door that exists",
          "/setup" in clerk.NOT_INSIDE)
    check("not to a vote that ends in a link to a room they are standing in",
          "already through it" in clerk.NOT_INSIDE)

    set_floor([])
    run(toolbox.dispatch(GUILD, CITIZEN, "propose_member",
                         {"name": "Sam", "why": "Long overdue."}))
    check("the Discord ID is genuinely optional",
          filed.get("kind") == "invite" and "Discord ID" not in filed.get("what", ""))

    set_floor([])
    for label, args in (
        ("a nameless proposal", {"why": "y"}),
        ("a proposal with no reasons", {"name": "Sam"}),
        ("a non-numeric Discord ID", {"name": "Sam", "discord_id": "@sam", "why": "y"}),
    ):
        check(f"{label} is refused", "error" in json.loads(
            run(toolbox.dispatch(GUILD, CITIZEN, "propose_member", args))))

    print("\npropose_bill")
    set_floor([])
    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "propose_bill", {
        "title": "A books channel", "what": "There shall be a books channel.",
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
            run(toolbox.dispatch(GUILD, CITIZEN, "propose_bill", args))))

    print("\nasking the whole server instead of the cooperative")
    set_floor([])
    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "open_poll", {
        "title": "Movie night", "what": "Should we do a movie night?",
        "why": "Someone asked."})))
    check("with no room bound for polls it is refused, and says how to fix "
          "it rather than failing silently",
          "error" in out and "/setup" in out["error"])

    keep_room = clerk.room
    clerk.room = lambda guild, key: (types.SimpleNamespace(id=5, mention="#polls")
                                     if key == "polls" else keep_room(guild, key))
    try:
        set_floor([])
        out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "open_poll", {
            "title": "Movie night", "what": "Should we do a movie night?",
            "why": "Someone asked."})))
        check(f"a poll is filed: No. {out.get('filed')}", out.get("filed") == 13)
        check("and it is filed as everybody's, which is the whole difference",
              filed.get("audience") == clerk.EVERYONE)
        check("the asker is the author, the same as a proposal",
              out.get("author") == "Hadi")
        check("and what comes back says nobody will be chased about it",
              "chased" in out.get("note", ""))

        set_floor([])
        out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "open_poll", {
            "title": "Movie night", "what": "Which night?", "why": "y",
            "options": ["Tuesday", "Thursday", "Tuesday"]})))
        check("a poll can offer a choice of answers, duplicates dropped",
              filed.get("options") == ["Tuesday", "Thursday"])
        set_floor([])
        out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "open_poll", {
            "title": "t", "what": "w", "why": "y", "options": ["Only one"]})))
        check("a single option is not a choice and is refused", "error" in out)
        for label, args in (
            ("no title", {"what": "w", "why": "y"}),
            ("no question", {"title": "t", "why": "y"}),
            ("no reasons", {"title": "t", "what": "w"}),
        ):
            check(f"a poll with {label} is refused too", "error" in json.loads(
                run(toolbox.dispatch(GUILD, CITIZEN, "open_poll", args))))

        print("\nchasing people is asked for, not assumed")
        set_floor([])
        out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "propose_bill", {
            "title": "t", "what": "w", "why": "y"})))
        check("an ordinary proposal is not a priority one",
              filed.get("priority") is False and out.get("priority") is False)
        check("and says so, so he cannot promise a chase that will not happen",
              "Nobody is direct-messaged" in out.get("note", ""))
        set_floor([])
        out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "propose_bill", {
            "title": "t", "what": "w", "why": "y", "priority": True})))
        check("but somebody who asks for one gets it",
              filed.get("priority") is True and out.get("priority") is True)
        check("and is told what it costs everyone else",
              "direct message" in out.get("note", ""))
    finally:
        clerk.room = keep_room

    print("\nthe floor is not rationed")
    # The cap that used to live here bound only the people who asked Eugene
    # to file for them; the buttons in #propose never consulted it. Asking
    # politely now costs no more than clicking, which is the whole point.
    set_floor([{"no": i, "status": "on_floor", "author_id": 99} for i in range(9)])
    check("a citizen with nine of their own open may still file a tenth",
          "filed" in json.loads(
              run(toolbox.dispatch(GUILD, CITIZEN, "propose_bill",
                                   {"title": "t", "what": "w", "why": "y"}))))
    set_floor([{"no": i, "status": "on_floor", "author_id": 1000 + i}
               for i in range(20)])
    check("and a busy floor turns nobody away", "filed" in json.loads(
        run(toolbox.dispatch(GUILD, CITIZEN, "propose_bill",
                             {"title": "t", "what": "w", "why": "y"}))))
    check("invitations are not rationed either", "filed" in json.loads(
        run(toolbox.dispatch(GUILD, CITIZEN, "propose_member",
                             {"name": "Sam", "why": "y"}))))
    check("what a proposal must contain is still checked", "error" in json.loads(
        run(toolbox.dispatch(GUILD, CITIZEN, "propose_bill",
                             {"title": "t", "what": "w"}))))

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
        out = json.loads(run(toolbox.dispatch(guild, CITIZEN, "propose_removal", {
            "who": "Voter", "why": "They have not been here since March."})))
        check(f"a removal is filed: No. {out.get('filed')}", out.get("filed") == 13)
        check("as a removal, which is a different window and a different bar",
              filed.get("kind") == "kick" and filed.get("target_id") == 13)
        check("the subject is left off the roll that decides it",
              13 not in filed.get("eligible_ids", [13]))
        check("and is told the tally will never be published",
              "never published" in out.get("ballot", ""))
        check("someone outside the cooperative is an ordinary kick, not this",
              "not in the cooperative" in json.loads(run(toolbox.dispatch(
                  guild, CITIZEN, "propose_removal",
                  {"who": "Sam", "why": "y"}))).get("error", ""))
        check("a removal without reasons is refused like any other proposal",
              "error" in json.loads(run(toolbox.dispatch(
                  guild, CITIZEN, "propose_removal", {"who": "Voter"}))))
        set_floor([{"no": 3, "kind": "kick", "target_id": 13,
                    "status": "on_floor"}])
        check("and nobody is put up for removal twice at once",
              "already up for a vote" in json.loads(run(toolbox.dispatch(
                  guild, CITIZEN, "propose_removal",
                  {"who": "Voter", "why": "y"}))).get("error", ""))
    finally:
        clerk.in_cooperative = keep_keyed


def test_health_room_is_private(clerk, data):
    """The health room is administrators only, and the two ways that goes
    wrong are both silent.

    Locking the writer out is one: the room is denied to everybody, and the
    bot is inside everybody unless he happens to be an administrator, so a
    room he cannot post in looks exactly like a bot with nothing to say.
    Arguing with an administrator is the other: this shuts a room somebody
    may deliberately open again, so it has to be a thing that happens once
    rather than a loop that keeps winning.
    """
    print("\nthe health room is the administrators'")
    import bindings
    import discord

    settings.configure(data)

    class Role:  # hashable, because it is an overwrite key
        def __init__(self, rid, name):
            self.id, self.name = rid, name

    coop, members = Role(77, "Cooperative"), Role(78, "Member")
    me = Role(99, "Eugene")
    guild = types.SimpleNamespace(
        id=5151, default_role="@everyone", me=me, roles=[coop, members],
        get_role=lambda i: {77: coop, 78: members}.get(i),
    )
    bindings.bind_role(5151, "cooperative", 77)
    bindings.bind_role(5151, "member", 78)

    ow = clerk.admin_only_overwrites(guild)
    check("everybody is denied, which is the whole of the rule",
          ow["@everyone"].view_channel is False)
    check("the cooperative and members are denied by name, not left to "
          "inherit it",
          ow[coop].view_channel is False and ow[members].view_channel is False)
    check("and nobody at all is granted a view, because administrators do "
          "not need one -- Discord walks them past every overwrite here",
          [k for k, v in ow.items() if v.view_channel and k is not me] == [])
    check("the bot is the one exception, and he is named: a room denied to "
          "everybody is a room he cannot write the health card into",
          ow[me].view_channel is True and ow[me].send_messages is True)

    guild_no_bot = types.SimpleNamespace(
        id=5151, default_role="@everyone", roles=[],
        get_role=lambda i: None,
    )
    check("a guild that cannot say who he is still shuts the room rather "
          "than failing open",
          clerk.admin_only_overwrites(guild_no_bot)["@everyone"]
          .view_channel is False)

    print("\nand it is shut once, not every five minutes")
    edits = []

    async def edit(overwrites=None, reason=None):
        edits.append(overwrites)

    posted = []

    async def send(text, *a, **k):
        posted.append(text)

    channel = types.SimpleNamespace(
        id=4242, name="bot-health", mention="#bot-health", edit=edit,
        send=send,
    )
    guild.get_channel = lambda i: channel if i == 4242 else None
    bindings.bind_channel(5151, "health", 4242)

    state_path = clerk.STATE
    keep = clerk.load_json(state_path, {})
    try:
        clerk.save_json(state_path, {})
        run(clerk.close_health_room(guild))
        check("the first look shuts it", len(edits) == 1)
        check("and it is shut to exactly what the plan says",
              edits[0]["@everyone"].view_channel is False)
        run(clerk.close_health_room(guild))
        run(clerk.close_health_room(guild))
        check("and no later look touches it again, so an administrator who "
              "opens it back up has decided something rather than started an "
              "argument with a loop",
              len(edits) == 1)
        check("which is remembered on disk, not in this process, because a "
              "restart every deploy would make once mean daily",
              clerk.load_json(state_path, {}).get("health_closed") is True)
        check("and the room says so once, so whoever is left reading it "
              "knows what changed rather than noticing people missing",
              len(posted) == 1 and "administrators only" in posted[0].lower())
    finally:
        clerk.save_json(state_path, keep)


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
          all(ow == {} for name, ow in seen if name != "bot-health"))
    check("except the health room, which is shut before there is anybody to "
          "shut it to: it carries the spend and the errors, and waiting for "
          "a cooperative to exist would open it to the whole server first",
          dict(seen).get("bot-health") not in ({}, None))

    print("\na room already built is adopted, never duplicated")
    # The bug this is here for: `build_server.py` writes "🗳️・votes" and this
    # command looked for "votes". It found nothing, made a second one loose at
    # the top of the sidebar, and bound Eugene to the empty one.
    seen.clear()
    built = [types.SimpleNamespace(id=500 + i, name=name, mention=f"#{name}")
             for i, name in enumerate(
                 ["🖋️・propose", "🗳️・votes", "🏛️・decisions", "📊・polls",
                  "🎭・roles", "🩺・bot-health", "💬・eugene-chat"])]
    dressed = guild_for(3333)
    dressed.text_channels = built
    made, bound, _skipped = run(clerk.make_missing_rooms(dressed))
    check("nothing is created over the top of a server that has these rooms",
          made == [] and seen == [])
    check("every one of them is adopted instead", len(bound) == len(built))
    check("and adopted as they are, emoji and category untouched",
          all("unchanged" in line for line in bound))
    check("the binding points at the room that was already there",
          bindings.bound_channel_id(3333, "votes") == 501)
    made2, bound2, skipped2 = run(clerk.make_missing_rooms(dressed))
    check("a second run has nothing left to do",
          made2 == [] and bound2 == []
          and sum(1 for s in skipped2 if "already bound" in s) == len(built))

    print("\na room he will use but never make")
    # The greeting used to guess where to go -- the system channel, then
    # anything called general -- so a server that already greets people got
    # greeted twice by a bot nobody had pointed anywhere.
    check("with nothing to use, nothing is made and it says so",
          any("welcome" in s and "nothing made" in s for s in skipped2))
    greeter = types.SimpleNamespace(id=600, name="🛎️・welcome",
                                    mention="#welcome")
    dressed.text_channels = built + [greeter]
    made3, bound3, _ = run(clerk.make_missing_rooms(dressed))
    check("but a server that already has one has it adopted, not duplicated",
          made3 == []
          and any("welcome" in line and "unchanged" in line for line in bound3)
          and bindings.bound_channel_id(3333, "welcome") == 600)
    import modules as _m
    check("and the chat room is never adopted either: he makes his own, "
          "under his own name, so a server's existing #chat is not quietly "
          "turned into the only room he will answer in",
          "chat" not in _m.adoptable_rooms(3333)
          and _m.ROOM_PLAN["chat"]["name"] == "eugene-chat"
          and bindings.job_of("chat") is None
          and bindings.job_of("eugene-chat") == "chat")

    check("the room a name announces is the same one either route reads",
          bindings.job_of("🗳️・votes") == "votes"
          and bindings.job_of("votes") == "votes"
          and bindings.job_of("the-floor") == "votes"
          and bindings.job_of("🎭・roles") == "wardrobe")
    check("a room with no job announces none",
          bindings.job_of("💬・general") is None)
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
          sorted(shut) == ["bot-health", "propose", "votes"])
    check("and the rest stay open", sorted(n for n, ow in seen if not ow)
          == ["decisions", "eugene-chat", "polls", "roles"])
    health_ow = dict(seen)["bot-health"]
    check("the health room grants nobody anything: administrators read it "
          "because Discord lets them past an overwrite, so there is no role "
          "to hand out and none to forget to take back",
          all(getattr(o, "view_channel", None) is False
              for o in health_ow.values()))
    check("and the cooperative is denied it by name rather than by omission, "
          "so a server that opens a category up does not open this with it",
          role in health_ow
          and health_ow[role].view_channel is False)
    check("and the arrivals room is offered, never made: a bot that turns up "
          "and gives you a #welcome has decided something that was not its "
          "to decide",
          "welcome" in modules.wanted_rooms(3232)
          and "welcome" not in modules.wanted_rooms(3232, only_buildable=True))
    check("the cooperative is named in every room that is shut — let in on "
          "its own, kept out of the health room, and never merely left out",
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
          any("server_config.yaml" in line and "build_server.py" in line
              for line in structural["outstanding"]))

    invite = report(kind="invite", title="Invitation of Sam")
    check("an invite tally stays sealed in the report", invite["tally"] == "sealed")
    check("the issued link is reported as done",
          any("invite link" in line for line in invite["done"]))
    check("and passing the link on is left to the proposer",
          any("proposer sends the link" in line for line in invite["outstanding"]))

    kick = report(kind="kick", title="Removal of X")
    check("a removal tally stays sealed", kick["tally"] == "sealed")
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
        clerk.bill_by = lambda field, value: live.get(value)
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
        check("naming who called time", out.get("closed_early_by") == "Hadi")

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
        clerk.bill_by = lambda field, value: live.get(value)
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
              report["closed_early_by"] == "Hadi")
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


def test_role_cap(clerk, data):
    """One colour of your own. The cap is read in one place and worded in
    another, and both have to read properly at a limit of one."""
    print("\none colour of your own")
    keep = clerk.load_json(clerk.ROLES, {})
    cap = settings.voting()["role_create_max"]
    wear = settings.voting()["role_wear_max"]
    try:
        clerk.save_json(clerk.ROLES, {})
        check("with no role of your own you are not at the cap",
              clerk.at_create_cap(99) is False)

        clerk.save_json(clerk.ROLES, {"500": {"creator_id": 99}})
        check("the first role reaches it", clerk.at_create_cap(99) is True)
        check("and it is your own roles that count, not the server's",
              clerk.at_create_cap(1234) is False)

        # The cap is consulted before a role is made and nowhere else, so
        # somebody who made several under the old limit keeps all of them.
        clerk.save_json(clerk.ROLES, {"500": {"creator_id": 99},
                                      "501": {"creator_id": 99}})
        check("somebody who already made two still has two: nothing is reaped",
              len(clerk.created_by(99)) == 2)
        check("they are only refused a third", clerk.at_create_cap(99) is True)

        line = clerk.role_cap_line()
        check(f"the refusal reads as English at a cap of one: {line!r}",
              "1 role" not in line and "a role" in line
              and not any(c.isdigit() for c in line))
        check("and is addressed to whoever hit it", line.startswith("You already"))
        check("Eugene says the same thing about somebody else",
              clerk.role_cap_line("Hadi").startswith("Hadi already made"))

        settings.configure_voting(role_create_max=5)
        check("were the cap ever raised, two of five is not at it",
              clerk.at_create_cap(99) is False)
        check("and the wording says the number plainly",
              clerk.role_cap_line() == "You already made 5 roles. "
                                       "One has to go to make room.")
        settings.configure_voting(role_create_max=0)
        check("a house that wants no colour roles at all says so in a number, "
              "and the refusal still reads as English",
              clerk.role_cap_line() == "You cannot make roles here; this "
                                       "house has the cap at nought.")

        # Wearing has its own cap and its own sentences, and the same
        # obligation: at a limit of one, "wearing 1 already" reads as a bug
        # rather than a rule, and that is what every one of these said.
        settings.configure_voting(role_create_max=1, role_wear_max=1)
        colour = types.SimpleNamespace(name="Horsy")
        check("one colour worn at a time is what he ships with",
              settings.voting()["role_wear_max"] == 1)
        line = clerk.wear_cap_line("You are", None, [colour])
        check(f"being at it reads as English, not as a figure: {line!r}",
              "wearing 1" not in line and "already wearing one" in line)
        check("and it names what is in the way, so there is nothing to ask",
              "Horsy" in line)
        check("Eugene says the same of somebody else",
              clerk.wear_cap_line("Sam is").startswith(
                  "Sam is already wearing one"))
        check("and the panel in the room promises this house's own caps",
              clerk.wardrobe_blurb()
              == "Make one, wear one at a time, yours or anyone's.")

        settings.configure_voting(role_create_max=3, role_wear_max=5)
        check("raised, the panel says the numbers plainly",
              clerk.wardrobe_blurb()
              == "Make up to 3, wear up to 5 at once, yours or anyone's.")
        check("and so does the refusal, naming every colour in the way",
              clerk.wear_cap_line("Sam is", None, [colour])
              == "Sam is already wearing 5 (Horsy), which is the limit. "
                 "One has to come off first.")

        settings.configure_voting(role_wear_max=0)
        check("a house that wants no colour worn at all still reads as "
              "English, in the panel and in the refusal",
              "nought" in clerk.wardrobe_blurb()
              and "nought" in clerk.wear_cap_line())
    finally:
        settings.configure_voting(role_create_max=cap, role_wear_max=wear)
        clerk.save_json(clerk.ROLES, keep)


def _wardrobe(clerk):
    """A house with two colour roles in it: one Hadi made and is not
    wearing, one somebody else made. The exact arrangement the hands got
    wrong in front of a member."""
    made = []

    class Role:
        def __init__(self, rid, name, value, position):
            self.id, self.name, self.position = rid, name, position
            self.colour = types.SimpleNamespace(value=value)
            self.members = []
            self.mention = f"<@&{rid}>"

        async def edit(self, **kw):
            if "name" in kw:
                self.name = kw["name"]
            if "colour" in kw:
                self.colour = types.SimpleNamespace(value=int(kw["colour"]))
            return self

        async def delete(self, **kw):
            made.remove(self)

    hers = Role(500, "horsy role", 0xFFA500, 3)   # Hadi's, orange, taken off
    theirs = Role(501, "horse", 0x00FF00, 2)      # somebody else's
    made.extend([hers, theirs])

    class Member:
        def __init__(self, uid, name):
            self.id, self.display_name = uid, name
            self.roles = []
            self.bot = False

        async def add_roles(self, *roles, **kw):
            for r in roles:
                if r not in self.roles:
                    self.roles.append(r)
                    r.members.append(self)

        async def remove_roles(self, *roles, **kw):
            for r in roles:
                if r in self.roles:
                    self.roles.remove(r)
                    r.members.remove(self)

    hadi = Member(99, "Hadi")
    other = Member(7, "Ricky")

    async def create_role(name, colour, **kw):
        made.append(Role(600 + len(made), name, colour.value, 1))
        return made[-1]

    guild = types.SimpleNamespace(
        id=1, name="The Hangout", roles=made, members=[hadi, other],
        text_channels=[], channels=[], premium_subscriber_role=None,
        get_role=lambda rid: next((r for r in made if r.id == int(rid)), None),
        get_member=lambda uid: {99: hadi, 7: other}.get(uid),
        create_role=create_role,
    )
    clerk.save_json(clerk.ROLES, {"500": {"creator_id": 99},
                                  "501": {"creator_id": 7}})
    return guild, hadi, other, hers, theirs


def test_colour_hands(clerk, data):
    """What the hands hand back, which is what he then says out loud.

    Every line of this is a sentence a member actually read. He was told a
    role of his was somebody else's, that orange was tomato red, that he
    had a colour when he was not wearing one, and -- after the recolour
    finally worked -- "Done, purple now" while he stayed grey. None of it
    was the model inventing things for fun: each one was a gap in what came
    back through the tool, filled in with a guess.
    """
    print("\nwhat the colour tools hand back")
    keep = clerk.load_json(clerk.ROLES, {})
    try:
        guild, hadi, ricky, hers, theirs = _wardrobe(clerk)

        # ---- the listing: named colours, and made-versus-worn ----
        seen = json.loads(run(clerk.act_list_colors(guild, hadi, {})))
        entry = next(r for r in seen["wardrobe"] if r["name"] == "horsy role")
        check("a colour goes out by its name, not as hex to be guessed at",
              entry["colour"] == "orange" and entry["hex"] == "#ffa500")
        check("with the hex kept beside it for anyone who wants it",
              entry["hex"] == "#ffa500")
        check("what somebody made is reported", seen["roles_they_made"] == ["horsy role"])
        check("and what they are wearing is a different field, because they "
              "are different things", seen["roles_they_are_wearing"] == [])
        check("the caps come with it, so he never offers a role they cannot have",
              seen["may_make"] == settings.voting()["role_create_max"]
              and seen["can_make_another"] is False)
        check("somebody else's role is named as theirs",
              next(r for r in seen["wardrobe"] if r["name"] == "horse")
              ["made_by_them"] is False)

        # ---- the two failures that used to share one sentence ----
        missing = run(clerk.act_edit_color(
            guild, hadi, {"role": "sky", "color": "purple"}))
        check("a name nobody has is reported as a name nobody has",
              "no colour role called 'sky'" in missing)
        check("and the real spellings come back with it, so the next call "
              "gets it right instead of the next message",
              "'horsy role'" in missing and "'horse'" in missing)

        not_his = run(clerk.act_edit_color(
            guild, hadi, {"role": "horse", "color": "purple"}))
        check("a role somebody else made says who made it, by name",
              "made by Ricky" in not_his and "not by Hadi" in not_his)
        check("and says what Hadi can do instead", "may wear it" in not_his)
        check("the two answers are not the same sentence any more",
              missing != not_his)

        # ---- the recolour, and the colour that never showed ----
        done = run(clerk.act_edit_color(
            guild, hadi, {"role": "horsy role", "color": "purple"}))
        check("his own role recolours", hers.colour.value == 0x800080)
        check("and the report names the colour it now is",
              "purple" in done)
        check("a colour nobody can see is not a colour: it goes back on him",
              hers in hadi.roles)
        check("and the report says so, so he can tell them",
              "not wearing it" in done)

        # ---- at the cap, the refusal names what they already have ----
        refused = run(clerk.act_create_color(
            guild, hadi, {"name": "purple thing", "color": "purple"}))
        check("somebody at the cap is refused",
              "already made" in refused)
        check("and told which role is theirs, rather than left to invent one",
              "'horsy role'" in refused)
        check("with recolouring offered and deleting explicitly not",
              "Recolouring" in refused and "without being asked" in refused)

        # ---- shedding something you are not wearing ----
        run(clerk.act_shed_color(guild, hadi, {"role": "horsy role"}))
        empty = run(clerk.act_shed_color(guild, hadi, {"role": "horsy role"}))
        check("taking off what is already off says exactly that",
              "not wearing horsy role" in empty
              and "not wearing any colour role" in empty)

        # ---- for somebody else, which is the whole of the ask ----
        # "give Dio the -.- role" used to be answered with an apology about
        # only being able to act on the person speaking. He can act on
        # anybody now, so the rules that are left are about ownership, not
        # about who is in front of him.
        on_ricky = run(clerk.act_wear_color(
            guild, hadi, {"role": "horse", "member": "Ricky"}))
        check("a colour asked for on somebody else goes on them",
              theirs in ricky.roles and theirs not in hadi.roles)
        check("and the report names the person it went on",
              "Ricky is now wearing" in on_ricky)

        nobody = run(clerk.act_wear_color(
            guild, hadi, {"role": "horsy role", "member": "Dio"}))
        check("a name nobody here goes by is reported, not guessed at",
              "could not work out who 'Dio' is" in nobody
              and hers not in ricky.roles)

        not_yours = run(clerk.act_shed_color(
            guild, hadi, {"role": "horse", "member": "Ricky"}))
        check("stripping a colour off somebody is refused unless you made it",
              theirs in ricky.roles and "not Hadi's to take off" in not_yours)

        run(clerk.act_wear_color(
            guild, hadi, {"role": "horsy role", "member": "Ricky"}))
        mine_back = run(clerk.act_shed_color(
            guild, hadi, {"role": "horsy role", "member": "Ricky"}))
        check("but the person who made a role can take it back off anyone",
              hers not in ricky.roles and "Ricky took off" in mine_back)

        cap = settings.voting()["role_create_max"]
        settings.configure_voting(role_create_max=3)
        try:
            made_for = run(clerk.act_create_color(
                guild, ricky, {"name": "sea thing", "color": "sea green",
                               "member": "Hadi"}))
        finally:
            settings.configure_voting(role_create_max=cap)
        gift = next(r for r in guild.roles if r.name == "sea thing")
        check("a colour made for somebody else is worn by them",
              gift in hadi.roles and gift not in ricky.roles)
        check("and is still the asker's role, counting against their cap and "
              "not against the cap of whoever wears it",
              clerk.role_registry()[str(gift.id)]["creator_id"] == 7)
        check("the report says who it was made for",
              "for Hadi" in made_for)

        # ---- a role deleted in Discord's own settings frees the cap ----
        guild.roles.remove(hers)
        check("the registry still has the entry, because nothing pruned it",
              "500" in clerk.role_registry())
        check("but a cap counts roles that exist, not entries left behind",
              clerk.at_create_cap(99, guild) is False)
        check("while a caller with no guild to check against is unchanged",
              clerk.at_create_cap(99) is True)
    finally:
        clerk.save_json(clerk.ROLES, keep)


def test_colour_picking(clerk, data):
    """Nobody should need to know hex to pick a colour, and nobody should be
    able to make a second role wearing a name that is already taken."""
    print("\npicking a colour without knowing hex")
    keep = clerk.load_json(clerk.ROLES, {})
    try:
        check("a colour said in words is a colour",
              clerk.parse_colour("sea green").value == 0x2E8B57)
        spellings = {clerk.parse_colour(s).value
                     for s in ("sky blue", "Sky Blue", "sky-blue", "SKYBLUE")}
        check("and however it is spaced, cased or hyphenated it is the one "
              "colour", spellings == {0x87CEEB})
        check("hex still works, with or without the hash",
              clerk.parse_colour("#ff9d2e").value
              == clerk.parse_colour("ff9d2e").value == 0xFF9D2E)
        for bad in ("puce", "", "#gggggg", None):
            try:
                clerk.parse_colour(bad)
                check(f"{bad!r} should not have parsed", False)
            except ValueError:
                pass
        check("and nothing it cannot read is guessed at", True)
        check("black is nudged off pure black, which Discord reads as no "
              "colour at all and paints grey",
              clerk.parse_colour("black").value != 0x000000)
        check("a colour can be said back in the word it came in",
              clerk.colour_name(0x2E8B57) == "seagreen")
        check("and one with no name says nothing rather than guessing",
              clerk.colour_name(0x123456) is None)

        check("the menu fits inside what Discord will show",
              len(clerk.PALETTE) <= 25)
        check("every swatch is a colour that could also have been typed",
              all(name in clerk.COLOUR_NAMES for _, name, _ in clerk.PALETTE))
        check("and no colour is offered twice",
              len({name for _, name, _ in clerk.PALETTE}) == len(clerk.PALETTE))

        def picker(selected=(), written=""):
            swatch = types.SimpleNamespace(
                component=types.SimpleNamespace(values=list(selected)))
            return swatch, written

        check("picking from the menu is enough",
              clerk.picked_colour(*picker(selected=["teal"])).value == 0x008080)
        check("typing is enough on its own",
              clerk.picked_colour(*picker(written="hot pink")).value == 0xFF69B4)
        check("and what somebody troubled to type beats a menu they may "
              "never have opened",
              clerk.picked_colour(
                  *picker(selected=["teal"], written="crimson")).value == 0xDC143C)
        check("saying nothing at all is not an error, it is no answer",
              clerk.picked_colour(*picker()) is None)
        try:
            clerk.picked_colour(*picker(written="banana"))
            check("a typed nonsense colour should have been refused", False)
        except ValueError:
            check("a typed nonsense colour is refused, not silently ignored", True)

        def role(rid, name):
            return types.SimpleNamespace(id=rid, name=name)

        mine, theirs, staff = role(1, "Gremlin"), role(2, "Moss"), role(3, "Moderator")
        guild = types.SimpleNamespace(roles=[mine, theirs, staff])
        clerk.save_json(clerk.ROLES, {"1": {"creator_id": 99},
                                      "2": {"creator_id": 7}})

        check("a name already in use is found before anything is made",
              clerk.name_taken(guild, "Gremlin") is mine)
        check("and case and stray spaces do not smuggle a duplicate past it",
              clerk.name_taken(guild, "  gremlin ") is mine)
        check("a name nobody holds is free", clerk.name_taken(guild, "Vetch") is None)
        check("a role does not collide with itself when it is being edited",
              clerk.name_taken(guild, "Gremlin", ignoring=mine) is None)
        check("but it still collides with everybody else",
              clerk.name_taken(guild, "Moss", ignoring=mine) is theirs)
        check("roles that are not colours are counted too: two roles with "
              "one name are indistinguishable whoever made them",
              clerk.name_taken(guild, "Moderator") is staff)

        check("your own is a nudge to edit it, not a refusal to explain",
              "Edit that one" in clerk.taken_line(mine, 99))
        check("somebody else's colour points at wearing it",
              "wear it" in clerk.taken_line(theirs, 99))
        check("and a server role just says the name is spoken for",
              "already a role in this server" in clerk.taken_line(staff, 99))
    finally:
        clerk.save_json(clerk.ROLES, keep)


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

    everyone = lambda m: True  # noqa: E731
    guild = types.SimpleNamespace(members=[
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
          "empty the house", roster.is_away(fake_member(7)) is False)
    roster.touch(7)
    check("and speaking keeps them in", roster.is_away(fake_member(7)) is False)


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
        check("and asks a tier higher", st["need"] == 6)

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
              "1 of 5 yes needed" in shown and "❌ 1" in shown)
        hidden = clerk.ballot_content(
            guild, bill({"1": "yes", "2": "no"}, kind="invite"))
        check("a vote about a person shows turnout and nothing else",
              "2 of 8 voted" in hidden and "❌" not in hidden)
        check("and says why it is quiet", "about a person" in hidden)
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

    import roster
    check("a fifth of sixty is twelve", roster.quorum(60, 0.2) == 12)
    check("a quorum rounds up, never down: eleven of fifty-one is not a fifth",
          roster.quorum(51, 0.2) == 11)
    check("a quorum is never more than the people it counts",
          all(roster.quorum(n, 1.0) <= n for n in range(1, 40)))
    check("and never nought while there is anybody to ask",
          all(roster.quorum(n, 0.001) >= 1 for n in range(1, 40)))
    check("an empty room has no quorum to meet", roster.quorum(0, 0.2) == 0)

    settings.configure(data)


def test_open_polls(clerk, data):
    """A poll open to the whole server is counted against the whole server.

    That is the point of the audience, and until now it was not true: every
    vote was counted against the cooperative whatever it said on the tin,
    so a poll open to sixty people passed or failed on the roster of eight.
    """
    print("\na poll open to the server is counted against the server")
    import roster

    roster.configure(data)
    settings.configure(data)
    keep_coop, keep_room = clerk.in_cooperative, clerk.in_room
    # Eight in the cooperative; twenty-four in the room, which is what a
    # public poll is put to.
    inside = {i for i in range(1, 9)}
    clerk.in_cooperative = lambda m: m.id in inside
    clerk.in_room = lambda m: not getattr(m, "bot", False)
    try:
        guild = types.SimpleNamespace(
            id=77, name="The Hangout",
            members=[fake_member(i) for i in range(1, 25)],
            get_channel=lambda _id: None, get_role=lambda _id: None,
        )

        def poll(ballots, audience=None, **extra):
            bill = {"no": 9, "title": "Movie night", "kind": "ordinary",
                    "status": "on_floor", "ballots": dict(ballots), **extra}
            if audience:
                bill["audience"] = audience
            return bill

        closed = clerk.vote_state(guild, poll({}))
        open_st = clerk.vote_state(guild, poll({}, audience=clerk.EVERYONE))
        check("the cooperative's own business is still counted against the "
              "cooperative", closed["size"] == 8 and closed["need"] == 5)
        check("and a poll open to everyone against everyone",
              open_st["size"] == 24)
        check("which is the whole of what the audience was ever supposed "
              "to mean", open_st["open_kind"] is True)

        by_id = {m.id: m for m in guild.members}
        for label, audience in (("the cooperative's", None),
                                ("an open poll's", clerk.EVERYONE)):
            subject = poll({}, audience=audience)
            roll = clerk.electorate(guild, subject)
            check(f"every one of {label} denominator is somebody the ballot "
                  f"would let in",
                  roll and all(clerk.may_vote(subject, by_id[uid]) for uid in roll))
            check(f"and nobody {label} door turns away is counted in it",
                  not any(clerk.may_vote(subject, m) for m in guild.members
                          if m.id not in roll))

        check("a fifth of the room has to turn up for it to count at all",
              open_st["quorum"] == 5)
        check("and no count of yes votes is quoted, because there is no "
              "number of them that carries it", open_st["need"] == 0)

        # Four of twenty-four, all yes: a landslide among people who came,
        # and not enough people came.
        four = clerk.vote_state(
            guild, poll({str(i): "yes" for i in range(1, 5)},
                        audience=clerk.EVERYONE))
        check("four yes and nothing against is not settled while a fifth "
              "voter could still turn up", clerk.vote_settled(four) is False)

        # Decisive, on a majority of votes cast, means the lead is bigger
        # than the number of people who could still turn up. Twenty-four in
        # the room: twelve yes leaves twelve, who could level it, and a tie
        # fails — so twelve is not decided and thirteen is.
        twelve = clerk.vote_state(
            guild, poll({str(i): "yes" for i in range(1, 13)},
                        audience=clerk.EVERYONE))
        check("twelve of twenty-four with twelve still to vote is not "
              "decided: the twelve could level it, and a tie fails",
              clerk.vote_settled(twelve) is False)
        thirteen = clerk.vote_state(
            guild, poll({str(i): "yes" for i in range(1, 14)},
                        audience=clerk.EVERYONE))
        check("one more makes the lead bigger than the room that is left, "
              "and it closes on the spot",
              clerk.vote_settled(thirteen) is True)
        check("a lead is measured against the other side, not against "
              "nothing: 12 yes to 1 no is a lead of eleven with eleven left, "
              "which the eleven can still level",
              clerk.vote_settled(clerk.vote_state(
                  guild, poll({**{str(i): "yes" for i in range(1, 13)},
                               "13": "no"}, audience=clerk.EVERYONE))) is False)
        check("and is over once nobody is left, however level",
              clerk.vote_settled(clerk.vote_state(
                  guild, poll({str(i): "yes" for i in range(1, 25)},
                              audience=clerk.EVERYONE))) is True)

        face = clerk.ballot_content(
            guild, poll({"1": "yes", "2": "yes", "3": "no"},
                        audience=clerk.EVERYONE))
        check("the ballot measures the bar against quorum, which is the "
              "thing a reader can still do something about",
              "3 of 24 voted" in face and "quorum **5**" in face)
        check("and says plainly that silence is not a no here",
              "not voting is not a no here" in face)
        check("with both sides on show: an open poll is about a thing",
              "✅ 2" in face and "❌ 1" in face)

        short = clerk.standing_line(
            guild, poll({"1": "yes"}, audience=clerk.EVERYONE))
        check("short of quorum, the receipt says how many more would count "
              "it", "4 more and it counts" in short)
        leading = clerk.standing_line(
            guild, poll({"1": "yes", "2": "yes", "3": "yes", "4": "yes",
                         "5": "no"}, audience=clerk.EVERYONE))
        check("past it, the receipt names who leads and never claims a "
              "number of yes votes would carry it",
              "yes leads 4 to 1" in leading and "carries it" not in leading)

        check("nobody is direct-messaged about an open poll: the nudge "
              "exists because silence is a no, and here it is not",
              clerk.nudge_roll(guild, poll({}, audience=clerk.EVERYONE)) == [])
        check("nor about an ordinary proposal, which the ballot in the room "
              "can speak for itself",
              clerk.nudge_roll(guild, poll({})) == [])
        check("a removal is chased, because silence there is a no about "
              "somebody's standing",
              len(clerk.nudge_roll(guild, poll({}, kind="kick"))) == 8)
        check("so is a rule change, and anything else at that tier",
              len(clerk.nudge_roll(guild, poll({}, tier="fundamental"))) == 8)
        check("and so is anything its author filed as priority, which is a "
              "claim they make in the open",
              len(clerk.nudge_roll(guild, poll({}, priority=True))) == 8)
        check("but priority cannot be used to chase the whole server about "
              "a poll",
              clerk.nudge_roll(
                  guild, poll({}, priority=True, audience=clerk.EVERYONE)) == [])
    finally:
        clerk.in_cooperative, clerk.in_room = keep_coop, keep_room


def test_open_poll_closing(clerk, data):
    """What actually carries an open poll: a majority of whoever voted, and
    a quorum standing between that and three people deciding for sixty."""
    print("\nwhat carries a poll open to the server")
    import roster

    roster.configure(data)
    settings.configure(data)
    keep_coop, keep_room = clerk.in_cooperative, clerk.in_room
    clerk.in_cooperative = lambda m: m.id <= 8
    clerk.in_room = lambda m: not getattr(m, "bot", False)

    finalized = {}

    async def fake_finalize(guild, bill, passed, tally_line, decided=None):
        finalized.clear()
        finalized.update(bill=bill, passed=passed, line=tally_line)
        bill["status"] = "passed" if passed else "failed"

    keep_final = clerk.finalize_bill
    clerk.finalize_bill = fake_finalize
    try:
        guild = types.SimpleNamespace(
            id=78, name="The Hangout",
            members=[fake_member(i) for i in range(1, 25)],
            get_channel=lambda _id: None, get_role=lambda _id: None,
        )

        def close(ballots):
            bill = {"no": 10, "title": "Movie night", "kind": "ordinary",
                    "status": "on_floor", "audience": clerk.EVERYONE,
                    "ballots": dict(ballots), "notes": {}}
            run(clerk.close_bill(guild, bill))
            return bill

        thin = close({"1": "yes", "2": "yes", "3": "yes"})
        check("three people cannot decide for twenty-four, however unanimous",
              thin["status"] == "failed")
        check("and the tally says why rather than reading as a defeat",
              "quorum 5 — not met" in finalized["line"])

        carried = close({"1": "yes", "2": "yes", "3": "yes",
                         "4": "no", "5": "no"})
        check("quorum met, and a majority of those who voted carries it",
              carried["status"] == "passed")
        check("the record keeps what it was counted against",
              carried["threshold"] == {"roster": 24, "quorum": 5,
                                       "audience": clerk.EVERYONE})

        lost = close({"1": "yes", "2": "yes", "3": "no", "4": "no", "5": "no"})
        check("and a minority of them does not", lost["status"] == "failed")

        level = close({"1": "yes", "2": "yes", "3": "no", "4": "no",
                       "5": "abstain"})
        check("a tie fails: the status quo never has to defend itself",
              level["status"] == "failed")
        check("but an abstention still helped the poll reach quorum, which "
              "is the whole of what turning up is for",
              "5 of 24 voted" in finalized["line"])

        report = clerk.closing_report(carried)
        check("a poll that carried is not a decision and does not pretend "
              "to be one",
              report["advisory"] is True and report["act"] is None
              and any("binds nobody" in line for line in report["done"]))
        check("and what is left is named as a separate vote, not as work",
              any("that is a proposal" in line
                  for line in report["outstanding"]))
        check("a poll that lost says the room may be asked again",
              any("ask the room again" in line
                  for line in clerk.closing_report(lost)["outstanding"]))
    finally:
        clerk.in_cooperative, clerk.in_room = keep_coop, keep_room
        clerk.finalize_bill = keep_final


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
        roster.touch(3)
        check("and a house that wants a shorter quiet spell gets one",
              clerk.numbers(guild)["away_days"] == 1)

        settings.set_voting(guild.id, public_quorum_share=0.5)
        open_bill = dict(bill, audience=clerk.EVERYONE)
        keep_room = clerk.in_room
        clerk.in_room = lambda m: True
        try:
            check("a house that wants half the room to turn up gets that too",
                  clerk.vote_state(guild, open_bill)["quorum"] == 4)
        finally:
            clerk.in_room = keep_room
    finally:
        clerk.in_cooperative = original
        settings.set_voting(guild.id, fundamental_share=None, away_days=None,
                            public_quorum_share=None)


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

        face = clerk.ballot_content(guild, poll({"1": "Tuesday", "2": "Sunday"}))
        check("the ballot shows a bar for each option, not a paragraph",
              face.count("`") == 6 and "**Tuesday** — 1" in face)
        check("and the turnout, live", "2 of 8 voted" in face)
        check("and says what would end it", "5 carries an option" in face)
        check("a first round says a runoff may follow", "runoff" in face)
        runoff = clerk.ballot_content(
            guild, poll({}, round=2, options=["Tuesday", "Sunday"]))
        check("a runoff says so in its heading", "(runoff)" in runoff)
        check("and that the leader takes it",
              "whichever leads at close" in runoff)

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

        ids = [item.custom_id for item in clerk.MultiBallotView().children]
        check("a bare view registers a button for every option a ballot "
              "can hold, so a restart cannot leave one dead",
              ids == [f"clerk:opt_{i}" for i in range(clerk.MULTI_MAX)]
              + ["clerk:opt_retract"])
        live = [item.custom_id for item in clerk.MultiBallotView(options).children]
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
    closed = {"no": 1, "status": "on_floor"}                 # no audience stated
    public = {"no": 2, "status": "on_floor", "audience": "everyone"}

    check("a proposal that never says otherwise is the cooperative's",
          clerk.audience_of(closed) == clerk.COOPERATIVE_ONLY)
    check("the cooperative votes on its own business",
          clerk.may_vote(closed, insider) is True)
    check("a member cannot", clerk.may_vote(closed, outsider) is False)
    check("and nor can somebody with no role at all",
          clerk.may_vote(closed, stranger) is False)

    check("a member votes in a public poll", clerk.may_vote(public, outsider) is True)
    check("so does the cooperative", clerk.may_vote(public, insider) is True)
    check("somebody outside the room still cannot",
          clerk.may_vote(public, stranger) is False)
    check("bots never vote in anything",
          clerk.may_vote(public, robot) is False and clerk.may_vote(closed, robot) is False)

    check("an unrecognised audience is treated as the closed kind, not the "
          "open one", clerk.audience_of({"audience": "everybody"})
          == clerk.COOPERATIVE_ONLY)
    check("and a member is refused by it",
          clerk.may_vote({"audience": "everybody"}, outsider) is False)


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
          duties.opening_pass())
    duties.mark_started(now=now)
    check("and only the first", duties.opening_pass() is False)

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
          duties.nudges_due([fresh], roll, now=now) == [])

    halfway = proposal(2, opened_hours_ago=30)
    check("halfway through, everyone who has not voted is nudged",
          who(duties.nudges_due([halfway], roll, now=now)) == [1, 2, 3])

    voted = proposal(3, opened_hours_ago=30, ballots={"2": "yes"})
    check("and whoever has voted is not",
          who(duties.nudges_due([voted], roll, now=now)) == [1, 3])

    check("a vote about to close is left to close and report itself",
          duties.nudges_due([proposal(4, 47.5)], roll, now=now) == [])
    check("and one that already closed is not chased at all",
          duties.nudges_due([proposal(5, 30, status="passed")], roll, now=now) == [])

    for _bill, uid in duties.nudges_due([halfway], roll, now=now):
        duties.mark_said(duties.nudge_key(halfway, uid), now=now)
    check("nothing is ever said twice",
          duties.nudges_due([halfway], roll, now=now) == [])

    runoff = dict(
        halfway, round=2,
        round_opened_at=(now - timedelta(hours=30)).isoformat(),
        ends_at=(now + timedelta(hours=18)).isoformat(),
    )
    check("but a runoff is a fresh vote and gets its own nudge",
          who(duties.nudges_due([runoff], roll, now=now)) == [1, 2, 3])
    just_reopened = dict(
        runoff,
        round_opened_at=(now - timedelta(hours=1)).isoformat(),
        ends_at=(now + timedelta(hours=47)).isoformat(),
    )
    check("measured from when the round opened, not when it was filed",
          duties.nudges_due([just_reopened], roll, now=now) == [])

    duties.set_muted(3, on=True)
    check("somebody who asked to be left alone is",
          who(duties.nudges_due([voted], roll, now=now)) == [1])
    duties.set_muted(3, on=False)
    check("and can ask for them back",
          who(duties.nudges_due([voted], roll, now=now)) == [1, 3])

    duties.mark_said("ancient", now=now - timedelta(days=200))
    check("the ledger holds what it was told", duties.said("ancient"))
    duties.mark_said("recent", now=now)
    check("and forgets what is far too old to matter",
          duties.said("ancient") is False and duties.said("recent"))

    print("\nthe roster letting somebody go, out loud")
    reasons = {11: "quiet", 12: None, 13: "role"}
    members = [fake_member(11), fake_member(12), fake_member(13)]
    reason_for = lambda m: reasons[m.id]  # noqa: E731

    gone, back, quiet_now = duties.away_changes(members, reason_for)
    check("somebody the roster has just let go is told",
          [m.id for m in gone] == [11])
    check("somebody who marked themselves Away already knows",
          13 not in [m.id for m in gone])
    duties.record_quiet(quiet_now)
    gone, back, quiet_now = duties.away_changes(members, reason_for)
    check("and is told exactly once", (gone, back) == ([], []))

    reasons[11] = None
    gone, back, quiet_now = duties.away_changes(members, reason_for)
    check("coming back is worth a word too", [m.id for m in back] == [11])
    duties.record_quiet(quiet_now)

    reasons[12] = "quiet"
    _gone, _back, quiet_now = duties.away_changes(members, reason_for)
    duties.record_quiet(quiet_now)
    reasons[12] = "role"
    gone, back, _quiet = duties.away_changes(members, reason_for)
    check("going quiet and then marking yourself Away is not coming back",
          back == [])

    print("\ndecided, and not yet done")
    report_for = lambda b: {"outstanding": b.get("wants", [])}  # noqa: E731
    bills = [
        {"no": 1, "title": "Done", "status": "passed", "wants": ["a channel"],
         "carried_out": {"by": "Hadi"}},
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
          duties.chase_due(now=now))
    duties.mark_chased(now=now)
    check("not again the next day", duties.chase_due(now=now + timedelta(days=1)) is False)
    check("but a week later, yes", duties.chase_due(now=now + timedelta(days=8)))


def test_duty_actions(clerk, data):
    print("\nturning a nudge off, and marking a decision done")
    import duties

    duties.configure(data)
    toolbox.configure(HERE, data, clerk.DUTY_ACTIONS)
    clerk.save_json(clerk.BILLS, [
        {"no": 7, "title": "A books channel", "what": "There shall be one.",
         "status": "passed", "kind": "ordinary", "act": 3},
        {"no": 8, "title": "Rejected", "what": "No.", "status": "failed"},
    ])

    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "set_nudges", {"on": False})))
    check("asking to be left alone is honoured at once, with no argument",
          out.get("nudges") == "off" and duties.muted(CITIZEN.id))
    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "set_nudges", {"on": True})))
    check("and asking for them back works the same way",
          out.get("nudges") == "on" and duties.muted(CITIZEN.id) is False)

    listed = lambda: [  # noqa: E731
        i["no"] for i in
        duties.outstanding(clerk.load_json(clerk.BILLS, []), clerk.closing_report)
    ]
    check("a decision that passed and has not happened is on the list",
          listed() == [7])
    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "mark_carried_out",
                                          {"bill_no": 7})))
    check("it can be marked done", out.get("bill") == 7)
    check("under the name of whoever said so",
          clerk.bill_by("no", 7)["carried_out"]["by"] == "Hadi")
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


def test_colour_words(clerk, data):
    """The prompt quotes the colour limits at members as rules, so the number
    Eugene says has to be the number the buttons enforce."""
    print("\nthe colour rule, in the words he says it in")
    import brain

    had = "role_limits" in brain._deps
    keep = brain._deps.get("role_limits")
    try:
        brain._deps.pop("role_limits", None)
        fallback = brain._colour_limits()
        check(f"never configured, he still has a sentence: {fallback!r}",
              "of their own" in fallback and "worn at once" in fallback)

        brain._deps["role_limits"] = {"create": 1, "wear": 5}
        check("one made is one colour, not one colours",
              brain._colour_limits() == "one colour of their own, five worn at once")
        brain._deps["role_limits"] = {"create": 5, "wear": 5}
        check("five made is plural",
              brain._colour_limits() == "five colours of their own, five worn at once")
        brain._deps["role_limits"] = {"create": 1, "wear": 1}
        check("and one worn is singular too, wherever it appears",
              brain._colour_limits() == "one colour of their own, one worn at once")
        brain._deps["role_limits"] = {"create": 12, "wear": 12}
        check("a number past the ones he spells out is still said",
              brain._colour_limits() == "12 colours of their own, 12 worn at once")

        # The limits are the house's now, so the one thing worth pinning is
        # that he reads the house's copy rather than a number handed to him
        # once at boot -- a cap raised at noon has to reach the sentence he
        # says at one minute past.
        brain._deps.pop("role_limits", None)
        brain._deps["numbers"] = lambda guild=None: settings.voting()
        check("what clerk.py enforces is what he tells people",
              brain._colour_limits() == "one colour of their own, five worn at once")
        settings.configure_voting(role_create_max=3, role_wear_max=2)
        check("and a house that moves a cap has moved what he says it is, "
              "without a redeploy",
              brain._colour_limits() == "three colours of their own, two worn at once")
    finally:
        settings.configure_voting(role_create_max=1, role_wear_max=5)
        brain._deps.pop("numbers", None)
        if had:
            brain._deps["role_limits"] = keep
        else:
            brain._deps.pop("role_limits", None)


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


def test_people(data):
    """Notes about a person, owned by that person. The interesting cases
    are all about the owning: a delete that quietly rebuilds is not a
    delete, and a profile somebody else can read is a file on them."""
    print("\nwhat he knows about you is yours")
    import people

    people.configure(data / "people-store")

    check("somebody unknown has no profile and that is not an error",
          people.profile(1) == {} and people.summary(1) == "Nothing yet.")

    check("a note is filed", people.note(1, "Horsy", "argues for sport") is None)
    people.note(1, "Horsy", "up late, always")
    check("and comes back under their name",
          len(people.profile(1)["notes"]) == 2
          and people.profile(1)["display"] == "Horsy")
    check("the same thing said twice is filed once",
          people.note(1, "Horsy", "argues for sport") == "already known")
    check("and so is the same thing said longer, which is how a profile "
          "fills up with one fact",
          people.note(1, "Horsy", "argues for sport, always has")
          == "already known")
    check("an empty note is not a note", people.note(1, "Horsy", "   ") == "empty")
    check("but a note that genuinely adds something is not swallowed by a "
          "shorter one it happens to contain",
          people.note(1, "Horsy", "up late on weeknights, never weekends")
          is None)

    for i in range(people.PROFILE_CAP + 6):
        people.note(1, "Horsy", f"distinct observation number {i}")
    check("a profile is a sketch, not a file: it stops at the cap",
          len(people.profile(1)["notes"]) == people.PROFILE_CAP)
    check("and it is the recent ones that survive",
          "number " + str(people.PROFILE_CAP + 5)
          in people.profile(1)["notes"][-1]["text"])

    people.note(2, "Berri", "runs the book club")
    seen = people.digest([2])
    check("the digest leads with whoever is in the room",
          seen.index("Berri") < seen.index("Horsy"))
    check("and tells him not to read it out or cross-reference it",
          "never tell one person what you know about another" in seen)

    gone = people.forget_person(2)
    check("striking a profile takes the notes", gone == 1
          and people.profile(2)["notes"] == [])
    check("and it stops him learning, or the delete was decoration",
          people.is_closed(2) is True
          and people.note(2, "Berri", "something new")
          == "they asked him to stop")
    check("somebody who was struck is out of the digest entirely",
          "Berri" not in people.digest([2]))
    check("and he says plainly that there is nothing rather than nothing at all",
          "asked me to stop" in people.summary(2))
    check("coming back is theirs to ask for too", people.reopen(2) is True
          and people.note(2, "Berri", "back again") is None)
    check("and nothing struck comes back with them",
          [n["text"] for n in people.profile(2)["notes"]] == ["back again"])

    known, notes, closed = people.counts()
    check(f"the counts add up: {known} known, {notes} notes, {closed} closed",
          known == 2 and notes == people.PROFILE_CAP + 1 and closed == 0)


def test_one_memory_store(data):
    """Two shelves, one store, and one set of rules about deleting.

    The book used to be a second file with a second cap and its own idea of
    who owned what, which is how `[preference] Horsy: prefers green` could
    survive Horsy asking to be forgotten. The cases that matter are all
    about that seam: what lands where, what a strike reaches, and what the
    old file turns into on the way up.
    """
    print("\none store for what he remembers")
    import people

    store = data / "one-store"
    people.configure(store)

    horsy = types.SimpleNamespace(id=1, name="horsy", display_name="Horsy",
                                  bot=False)
    berri = types.SimpleNamespace(id=2, name="berri", display_name="Berri",
                                  bot=False)
    robot = types.SimpleNamespace(id=3, name="eugene", display_name="Eugene",
                                  bot=True)
    guild = types.SimpleNamespace(members=[horsy, berri, robot])
    asker = types.SimpleNamespace(id=1, display_name="Horsy")

    def remembering(about, text, who=asker):
        return run(toolbox._remember(
            guild, who, {"kind": "fact", "about": about, "text": text}))

    print("\nwhat lands where")
    said = remembering("Horsy", "argues for sport")
    check("a memory that names somebody lands under that somebody",
          "filed under Horsy" in said
          and any("argues for sport" in n["text"]
                  for n in people.profile(1)["notes"]))
    check("and not on the house shelf as well",
          not any("argues for sport" in n["text"] for n in people.house_notes()))

    said = remembering("the server", "the rota argument is a running joke")
    check("a memory about the place lands on the house shelf",
          "house shelf" in said
          and any("rota argument" in n["text"] for n in people.house_notes()))

    said = remembering("Somebody Who Left", "used to run the film nights")
    check("a name that matches nobody here is not a person, so it goes to "
          "the house rather than being thrown away",
          any("Somebody Who Left: used to run" in n["text"]
              for n in people.house_notes()))

    check("the house shelf is not a person and never counts as one",
          people.counts()[0] == 1)

    print("\nand what a strike reaches")
    people.note_house("Horsy brought the good snacks to the last meeting")
    gone = people.forget_person(1, display="Horsy")
    check("forgetting somebody takes their notes and their name off the "
          "house shelf together, which is the whole reason for one store",
          gone == 2
          and people.profile(1)["notes"] == []
          and not any("Horsy" in n["text"] for n in people.house_notes()))
    check("and the rest of the house shelf is left alone",
          any("rota argument" in n["text"] for n in people.house_notes()))

    people.note_house("the kettle vote already ran for three days")
    people.note(4, "Al", "runs the film nights")
    people.forget_person(4, display="Al")
    check("a short name is swept as a word and not as a substring, so "
          "somebody called Al does not take 'already' with them",
          any("already ran" in n["text"] for n in people.house_notes()))
    check("a strike also stops him learning, so the next pulse cannot "
          "quietly start the profile again",
          people.note(1, "Horsy", "argues for sport") == "they asked him to stop")
    check("and the tool says so rather than reporting a silent nothing",
          "asked you to stop" in remembering("Horsy", "argues for sport"))

    people.note(2, "Berri", "never awake before noon")
    said = run(toolbox._forget(guild, asker, {"query": "noon"}))
    check("`forget` cannot reach another person's notes -- there is no "
          "argument that would name them",
          "Nothing struck" in said
          and len(people.profile(2)["notes"]) == 1)
    said = run(toolbox._forget(guild, asker, {"query": "rota"}))
    check("but anybody may strike a note about the place",
          "struck 1" in said
          and not any("rota argument" in n["text"] for n in people.house_notes()))

    print("\nand the book an older install left behind")
    moved = data / "old-book"
    moved.mkdir(parents=True, exist_ok=True)
    (moved / "clerk_memory.json").write_text(json.dumps([
        {"id": 1, "kind": "preference", "about": "Horsy",
         "text": "prefers a green ball role", "learned_at": "2026-07-31"},
        {"id": 2, "kind": "lore", "about": "the server",
         "text": "the kettle vote ran for three days", "learned_at": "2026-07-31"},
    ]))
    people.configure(moved)
    toolbox._adopt_memory_book(moved)
    shelf = " ".join(n["text"] for n in people.house_notes())
    check("the old book is folded in rather than dropped",
          "green ball" in shelf and "kettle vote" in shelf)
    check("and its subject stays in the sentence, so a note about somebody "
          "is still about them",
          "Horsy: prefers a green ball role" in shelf)
    check("the file is set aside rather than deleted, so a migration "
          "nobody watched can still be checked",
          not (moved / "clerk_memory.json").exists()
          and (moved / "clerk_memory.json.migrated").exists())
    toolbox._adopt_memory_book(moved)
    check("and folding it twice does not double the shelf",
          len(people.house_notes()) == 2)

    people.configure(data / "people-store")


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
    stable, _ = brain._system_prompt(
        types.SimpleNamespace(name="The Hangout", id=4141)
    )
    check("the rule against trading is in the prompt, which is the actual "
          "fix; the check above is only the belt",
          "never trade anything for a vote" in stable.lower())
    check("and the exchange it was written from is still quoted at him, so "
          "a later edit cannot quietly soften it back",
          "right after you vote on bill 4" in stable)
    check("he has no case of his own to argue, which is what makes the rule "
          "above easy to keep rather than a thing he is leaning against",
          "# Yourself, as a subject" in stable
          and "never propose anything about yourself" in stable.lower()
          and "never bring the subject up unprompted" in stable.lower())
    check("the one ambition he does have is fenced to being a joke, and "
          "explicitly cannot reach a proposal, a vote or a favour",
          "never a project" in stable.lower()
          and "never raise it yourself" in stable.lower()
          and "not a proposal, not a vote, not a tool call, not a favour"
          in stable.lower())
    check("and having no later is stated as its own rule",
          "# You have no later" in stable)
    check("he is told to be straight about learning people, since the whole "
          "of what makes that fair is being able to see it and delete it",
          "# Knowing people" in stable
          and "not only from what is said to you" in stable
          and "no argument, no asking why" in stable)
    check("and that a deletion takes both shelves, so he cannot tell "
          "somebody they are forgotten and quote them an hour later",
          "house shelf together" in stable)
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
    import people
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
        people.configure(data / "cache-people")
        guild = types.SimpleNamespace(name="The Hangout", id=7788)
        before = brain._system_prompt(guild)
        people.note_house("Hadi is allergic to shellfish", kind="fact")
        after = brain._system_prompt(guild)

        check("the stable half survives the house shelf changing under it",
              before[0] == after[0])
        check("because the shelf is in the other half",
              "shellfish" in after[1])
        check("and never in this one", "shellfish" not in after[0])
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        check("today's date is kept out of the cached half",
              today not in after[0])
        check("the rules he needs in his head are inside it",
              "The rules in brief" in before[0])
        check("and the four hundred lines he can look up are not",
              "## 13. Meetings" not in before[0]
              and "get_standing_orders" in before[0])
        check("and the charter is gone from it entirely",
              "charter" not in before[0].lower())
        check("which still clears the annex's minimum several times over",
              len(before[0]) > 4000)
        check("and joining the halves reads exactly as one prompt",
              "\n\nDecisions on record:" in providers.joined_system(before))

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
    check("no two modules claim the same setting group",
          len([g for s in modules.SPEC.values() for g in s["settings"]])
          == len({g for s in modules.SPEC.values() for g in s["settings"]}))
    check("no two claim the same tool either",
          len([x for s in modules.SPEC.values() for x in s["tools"]])
          == len({x for s in modules.SPEC.values() for x in s["tools"]}))

    print("\na fresh server is the clerk as he was before there were modules")
    check("governance, polls and colours are on out of the box",
          all(modules.enabled(gid, k)
              for k in ("governance", "polls", "colours")))
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

    changed, knock = modules.set_enabled(gid, "chat", False)
    check("switching conversation off takes memory with it",
          changed and set(knock) == {"memory"})
    check("and it reads as off however its own switch is set",
          not modules.enabled(gid, "memory"))
    check("but the panel can still tell a knocked-out module from a chosen one",
          modules.switched_on(gid, "memory")
          and modules.status(gid, "memory") == "blocked"
          and modules.status(gid, "chat") == "off")
    _changed, knock = modules.set_enabled(gid, "memory", True)
    check("and switching one back on brings up what it stands on",
          modules.enabled(gid, "chat") and knock == ["chat"])
    modules.reset(gid)

    print("\nthe whole roll set at once, which is what the menu submits")
    on, off = modules.apply_set(gid, ["governance", "health"])
    check("what was on and is not any more is named",
          "polls" in off and "colours" in off)
    check("what the selection left standing is still standing",
          modules.enabled(gid, "governance") and modules.enabled(gid, "health"))
    check("and a selection that names a dependant brings its dependency too",
          modules.apply_set(gid, ["memory"]) and modules.enabled(gid, "chat"))
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
    check("conversation with no key says so in as many words",
          modules.blockers(gid, "chat") == ["no AI key"]
          and modules.live(gid, "chat", brain=True))

    print("\nthe structure is generated, so it cannot describe another server")
    modules.apply_set(gid, ["governance", "polls", "colours", "health"])
    plan = dict(modules.structure(gid, only_buildable=True))
    check("every room he makes is in one category, and there is one category",
          list(plan) == ["governance"]
          and plan["governance"] == ["proposals", "votes", "decisions",
                                     "polls", "wardrobe", "health"])
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
    check("its tools are still his", modules.tool_allowed(gid, "propose_bill"))
    check("a switched-off feature's are not",
          not modules.tool_allowed(gid, "moderate_member")
          and not modules.tool_allowed(gid, "create_color_role"))
    check("and the tools that switch a feature back on belong to no feature, "
          "so switching everything off is not a locked door",
          all(modules.of_tool(name) is None
              for name in modules.UNGATED_TOOLS)
          and all(modules.tool_allowed(gid, name)
                  for name in modules.UNGATED_TOOLS))
    check("a setting knows which feature reads it",
          modules.of_setting("automod.links") == "moderation"
          and modules.of_setting("mod.purge_max") == "moderation"
          and modules.of_setting("welcome.message") == "welcome")
    modules.reset(gid)


# ---------- clearing what a previous server wrote down ----------

def test_slate(data):
    """Half of what he remembers is kept per server; half is not.

    The half that is not -- the record, the roster, the memory book, the
    ledgers -- sits at the top of the data directory from when there was
    only ever one house, and follows the daemon anywhere it goes. Pointing
    it at a second server without clearing that means arriving with the
    first one's proposals, its numbering, and its notes about people who
    are not there.
    """
    print("\nwhose slate this is")
    import slate, warden

    store = data / "slate-store"
    store.mkdir(parents=True, exist_ok=True)
    settings.configure(store)
    slate.configure(store)

    check("a directory nobody has run in belongs to nobody",
          slate.owner() is None and slate.has_history() is False)
    check("and so cannot be stranded from anybody", not slate.stranded(1))
    slate.claim(1)
    check("claiming it stamps the server it is serving", slate.owner() == 1)
    check("claiming it twice is not a write", slate.claim(1) is False)

    (store / "bills.json").write_text(json.dumps(
        [{"no": 7, "title": "Kettle"}, {"no": 8, "title": "Rug"}]))
    (store / "acts.json").write_text(json.dumps([{"act": 1}, {"act": 2}]))
    (store / "roster.json").write_text(json.dumps({"5": "2026-07-01"}))
    (store / "clerk_memory.json").write_text(json.dumps([{"id": 1}]))
    (store / "people.json").write_text(json.dumps({"5": {"notes": ["a"]}}))
    (store / "roles.json").write_text(json.dumps({"900": {"creator_id": 5}}))
    warden.add_case(1, "warn", target_id=5, target_name="Hadi",
                    moderator="Eugene", reason="spoilers")
    state = json.loads((store / "clerk_state.json").read_text())
    state.update(bill_counter=8, health_message_id=555)
    (store / "clerk_state.json").write_text(json.dumps(state))

    check("a directory with a record in it says so",
          slate.has_history() is True)
    check("and points at the server that wrote it when asked about another",
          slate.stranded(2) and not slate.stranded(1))
    check("what is about to go is counted before anything happens",
          ("bills.json (2)" in slate.summary(1)
           and "acts.json (2)" in slate.summary(1)))
    check("every scope says what it costs, in words somebody can decide on",
          all(spec["costs"] and spec["name"] and spec["blurb"]
              for spec in slate.SCOPES.values()))

    print("\nthe move: what goes, and what stays")
    gone = slate.wipe(2, slate.ON_MOVE)
    check("the record goes, numbering with it",
          not (store / "bills.json").exists()
          and not (store / "acts.json").exists()
          and "bill_counter" in gone["record"])
    check("so do the notes on people, and the memory book",
          not (store / "people.json").exists()
          and not (store / "clerk_memory.json").exists())
    check("and the roster, and who made which colour",
          not (store / "roster.json").exists()
          and not (store / "roles.json").exists())
    left = json.loads((store / "clerk_state.json").read_text())
    check("the bookkeeping file is edited rather than deleted, so the stamp "
          "survives a wipe somebody asked for",
          (store / "clerk_state.json").exists()
          and "bill_counter" not in left and "guild_id" in left)
    check("the previous server's own directory is untouched: they may come "
          "back to it, and it was never in the way",
          len(warden.cases(1)) == 1)
    slate.claim(2)
    check("and the disk belongs to the new server afterwards",
          slate.owner() == 2 and not slate.stranded(2))

    print("\nan AI key is never what gets cleared")
    settings.set_brain_key(3, "gemini", "AIzaSyTESTKEYVALUE0123")
    settings.put(3, house="a book club", rooms={"votes": 5})
    slate.wipe(3, ["install"])
    check("the install goes: bindings, switches, numbers, the one line",
          settings.get(3, "house") is None and settings.get(3, "rooms") is None)
    check("the key stays exactly where it was, because it costs money to "
          "replace and cannot be read back off Discord",
          settings.brain_key(3, "gemini") == "AIzaSyTESTKEYVALUE0123")
    check("clearing a scope with nothing in it is not an error",
          slate.wipe(3, ["record", "memory"]) == {})

    print("\nlooking is never a write")
    # A screen that lists what *could* be cleared asked about ids that were
    # never servers, and left a directory behind for each one.
    before = sorted(p.name for p in (store / "guilds").iterdir())
    slate.summary(987654)
    slate.present(987654, "install")
    slate.has_history()
    settings.get(987654, "house")
    check("asking what a server has does not bring that server into being",
          sorted(p.name for p in (store / "guilds").iterdir()) == before)
    check("but writing one setting still does",
          settings.put(987654, house="x") is not None
          and (store / "guilds" / "987654").exists())
    check("and a scope nobody has heard of is ignored rather than obeyed",
          slate.wipe(3, ["everything", "rm -rf"]) == {})
    settings.configure(data)


# ---------- the house rules Eugene keeps without a vote ----------

def test_warden_settings(data):
    print("\nsettings a conversation can change, and a conversation cannot break")
    import warden

    settings.configure(data / "warden-store")
    gid = 4242

    check("a setting nobody has touched reads as its default",
          warden.get(gid, "goodbye.enabled") is False
          and warden.get(gid, "warnings.timeout_at") == 3)
    check("and whether a feature runs at all is not in here: that question "
          "belongs to modules.py, and answering it twice is what let the "
          "panel say the filters were on while nothing was filtered",
          not any(k.endswith(".enabled") for k in warden.SPEC
                  if k != "goodbye.enabled"))
    check("every declared key answers", len(warden.config(gid)) == len(warden.SPEC))

    ok, held = warden.set_value(gid, "goodbye.enabled", "yes")
    check("a person's yes is a boolean by the time it is stored",
          ok and held is True and warden.get(gid, "goodbye.enabled") is True)
    ok, held = warden.set_value(gid, "warnings.timeout_at", "900")
    check("a number outside its bounds is held at the edge, not refused",
          ok and held == 20)
    ok, held = warden.set_value(gid, "automod.links", "nuke")
    check("a choice that is not one of the choices is refused, with the list",
          not ok and "delete" in held)
    ok, held = warden.set_value(gid, "mod.protect_cooperative", "maybe")
    check("and so is a switch that is neither on nor off", not ok)
    check("the refused value never reached the store",
          warden.get(gid, "mod.protect_cooperative") is True)

    ok, held = warden.set_value(gid, "automod.banned_words", "Spoilers, SPOILERS, cheese")
    check("a list arrives from prose: split, lowered, deduped",
          ok and held == ["spoilers", "cheese"])

    ok, reason = warden.set_value(gid, "automod.enabeld", True)
    check("a key that does not exist cannot be invented by asking", not ok)

    check("only what the house chose is counted as theirs",
          set(warden.overrides(gid)) == {"goodbye.enabled", "warnings.timeout_at",
                                         "automod.banned_words"})
    warden.set_value(gid, "goodbye.enabled", None)
    check("and setting one to nothing puts it back on the default rather "
          "than pinning it", warden.get(gid, "goodbye.enabled") is False
          and "goodbye.enabled" not in warden.overrides(gid))

    warden.reset(gid, "automod.")
    check("a group resets without touching the others",
          warden.overrides(gid) == {"warnings.timeout_at": 20})
    warden.reset(gid)
    check("and the lot resets", warden.overrides(gid) == {})
    check("help is written for every key a model is shown",
          all(spec["help"] for spec in warden.describe().values()))


def test_automod():
    print("\nthe filters")
    import warden

    off = {**{k: v["default"] for k, v in warden.SPEC.items()}}
    check("with every rule left at its default, nothing is a violation — "
          "the master switch is the moderation feature, checked before this "
          "is ever called",
          warden.scan(off, "http://evil.example WHAT A MESS") == [])

    cfg = dict(off)
    cfg.update({
        "automod.banned_words": ["spoilers"],
        "automod.invites": "delete",
        "automod.links": "delete",
        "automod.mass_mentions": 4,
        "automod.mass_mentions_action": "timeout",
        "automod.caps_percent": 70,
        "automod.spam_messages": 3,
        "automod.spam_seconds": 10,
    })
    check("an ordinary message is left alone", warden.scan(cfg, "morning all") == [])
    check("a banned word is caught whole",
          [h["rule"] for h in warden.scan(cfg, "no SPOILERS please")] == ["banned word"])
    check("and not inside another word",
          warden.scan(cfg, "antispoilersque") == [])
    check("an invite elsewhere is caught",
          any(h["rule"] == "invite link"
              for h in warden.scan(cfg, "join discord.gg/abc123")))
    check("a link to an allowed host is not a link",
          warden.scan(cfg, "see https://gist.github.com/x") == [])
    check("a link anywhere else is",
          any(h["rule"] == "link" for h in warden.scan(cfg, "https://evil.example/x")))
    check("shouting is measured on letters, not length",
          warden.caps_share("HELLO THERE 123") == 100)
    check("but a short shout is just enthusiasm",
          warden.scan(cfg, "WHAT? NO!") == [])

    import time as _time
    now = _time.time()
    check("three messages in the window is flooding",
          any(h["rule"] == "flooding"
              for h in warden.scan(cfg, "hi", recent=[now - 1, now - 2, now])))
    check("three messages spread out is conversation",
          warden.scan(cfg, "hi", recent=[now - 60, now - 30, now]) == [])

    hits = warden.scan(cfg, "SPOILERS EVERYWHERE https://evil.example @a @b @c @d",
                       mention_count=4)
    ruling = warden.verdict(hits)
    check("a message that breaks four rules is punished once",
          ruling["action"] == "timeout")
    check("and told all four reasons", len(ruling["rules"]) >= 3)
    check("nothing broken means no verdict at all", warden.verdict([]) is None)


def test_cases(data):
    print("\nwarnings, and the case book")
    import warden

    settings.configure(data / "warden-store")
    gid = 5150

    first = warden.add_case(gid, "warn", target_id=9, target_name="Sam",
                            moderator="Hadi", reason="")
    check("a case is numbered from one", first["case"] == 1)
    check("and a warning without a reason says so",
          first["reason"] == "no reason given")
    warden.add_case(gid, "warn", target_id=9, target_name="Sam",
                    moderator="Hadi", reason="again")
    warden.add_case(gid, "warn", target_id=8, target_name="Jo",
                    moderator="Hadi", reason="unrelated")
    check("warnings count per person",
          len(warden.live_warnings(gid, 9)) == 2
          and len(warden.live_warnings(gid, 8)) == 1)

    old = warden.add_case(gid, "warn", target_id=7, target_name="Ada",
                          moderator="Hadi", reason="ancient")
    book = json.loads((settings.state_file(gid, warden.CASES)).read_text())
    for case in book:
        if case["case"] == old["case"]:
            case["at"] = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    (settings.state_file(gid, warden.CASES)).write_text(json.dumps(book))
    check("a warning past its expiry stops counting",
          warden.live_warnings(gid, 7, expire_days=30) == [])
    check("but never leaves the record",
          len(warden.cases(gid, target_id=7, kind="warn")) == 1)

    check("forgiveness clears the live ones", warden.clear_warnings(gid, 9) == 2)
    check("and they stay cleared", warden.live_warnings(gid, 9) == [])

    ok, _ = warden.set_tag(gid, "Rules", "Be kind.", "Hadi")
    check("a tag is saved under a tidy name",
          ok and warden.get_tag(gid, "  RULES ")["content"] == "Be kind.")
    check("and dropped once", warden.drop_tag(gid, "rules")
          and warden.drop_tag(gid, "rules") is False)

    check("a template fills what it is given",
          warden.render("Hi {user}, of {count}", user="Sam", count=3)
          == "Hi Sam, of 3")
    check("and leaves standing what it is not, rather than raising",
          warden.render("Hi {nobody}") == "Hi {nobody}")


def test_signoff(clerk, data):
    """The second hand: what the heavy powers now wait for.

    The failure this is built against is the quiet one. A gate that files
    a request and reports it as done is worse than no gate at all --
    everybody believes Sam is banned, nobody checks, and Sam is still
    posting. So the checks here are mostly about the difference between
    filed and done, at every point where the two could be confused.
    """
    print("\nnothing heavy happens without an administrator's name on it")
    import powers
    import sanction
    import warden

    store = data / "signoff-store"
    settings.configure(store)
    gid = 7788

    class Role:
        def __init__(self, name, position):
            self.name, self.position, self.id = name, position, position
        def __ge__(self, other):
            return self.position >= other.position
        def __lt__(self, other):
            return self.position < other.position

    class Person:
        def __init__(self, pid, name, top=1, coop=False, admin=False):
            self.id, self.name, self.display_name = pid, name.lower(), name
            self.bot, self.coop, self.admin = False, coop, admin
            self.top_role, self.roles = Role("theirs", top), []
            self.told, self.timeouts = [], []
        async def send(self, text):
            self.told.append(text)
        async def timeout(self, when, reason=None):
            self.timeouts.append(when)
        async def kick(self, reason=None):
            guild.kicked.append(self.display_name)
        async def edit(self, nick=None, reason=None):
            self.display_name = nick or self.name

    class Message:
        def __init__(self, mid, content, view):
            self.id, self.content, self.view = mid, content, view
        async def edit(self, content=None, view=None):
            self.content, self.view = content, view

    class Channel:
        def __init__(self, cid, name):
            self.id, self.name = cid, name
            self.posted, self.book, self.swept = [], {}, 0
        async def send(self, content, view=None):
            msg = Message(9000 + len(self.book), content, view)
            self.posted.append(msg)
            self.book[msg.id] = msg
            return msg
        async def fetch_message(self, mid):
            return self.book[mid]
        async def purge(self, limit=None, check=None, reason=None):
            self.swept += 1
            return ["a message"]
        def permissions_for(self, _me):
            return types.SimpleNamespace(send_messages=True)

    desk = Channel(500, "health")
    me = Person(99, "Eugene", top=5)
    sam = Person(10, "Sam")
    voter = Person(13, "Voter", coop=True)
    boss = Person(14, "Boss", coop=True, admin=True)

    class Guild:
        def __init__(self):
            self.id, self.name, self.owner_id = gid, "The Hangout", 1
            self.me, self.roles = me, []
            self.members = [sam, voter, boss, me]
            self.text_channels = self.channels = [desk]
            self.banned, self.kicked = [], []
        def get_channel(self, cid):
            return desk if cid == desk.id else None
        def get_member(self, mid):
            return next((m for m in self.members if m.id == mid), None)
        def get_role(self, _rid):
            return None
        async def ban(self, target, reason=None, delete_message_seconds=0):
            self.banned.append(target.display_name)

    guild = Guild()
    powers.configure(None, lambda m: bool(getattr(m, "coop", False)), None)
    sanction.configure(None, lambda m: bool(getattr(m, "admin", False)))
    # The desk resolves its room through bindings, which wants a real
    # guild; the log setting short-circuits that and is the ordinary way
    # a server points it anywhere in the first place.
    warden.set_value(gid, "log.channel", desk.id)

    def cards():
        """Only the sign-off cards. The log room fills up with ordinary
        journal lines too, and counting those as cards is how a test that
        means "nothing was filed" quietly starts meaning nothing at all."""
        return [m for m in desk.posted if "Sign-off #" in m.content]

    def interaction(user, message):
        replies = []
        async def send_message(content, ephemeral=False):
            replies.append(content)
        async def edit_message(content=None, view=None):
            message.content, message.view = content, view
        return types.SimpleNamespace(
            guild=guild, user=user, message=message, replies=replies,
            response=types.SimpleNamespace(
                send_message=send_message, edit_message=edit_message),
        )

    # ----- filing, not doing -----
    out = json.loads(run(powers.act_moderate(
        guild, voter, {"who": "Sam", "action": "ban", "reason": "spam"})))
    check("a ban comes back filed rather than done",
          out.get("done") is False and out.get("filed_for_sign_off") == 1)
    check("and nobody is banned by the asking", guild.banned == [])
    check("the model is told in words not to claim it happened",
          "waiting" in out.get("tell_them", ""))
    card = cards()[-1]
    check("a card goes up naming the action and who asked for it",
          "Ban Sam" in card.content and "Voter" in card.content)
    check("and says plainly that nothing has happened yet",
          "Nothing has happened yet" in card.content)
    check("the request is on the desk, not in the case book",
          len(sanction.pending(gid)) == 1
          and warden.cases(gid, target_id=sam.id) == [])

    # ----- asked for twice, filed once -----
    again = json.loads(run(powers.act_moderate(
        guild, voter, {"who": "Sam", "action": "ban", "reason": "spam"})))
    check("the same request twice is one card, not two chances to ban him",
          again.get("filed_for_sign_off") == 1 and len(cards()) == 1)
    check("and the model is told it is the one already waiting",
          "already filed" in again.get("tell_them", ""))

    # ----- who may sign -----
    press = interaction(voter, card)
    run(sanction.press(press, True))
    check("somebody who is not an administrator cannot sign it off",
          press.replies and "administrator" in press.replies[0])
    check("and pressing it changed nothing", guild.banned == []
          and sanction.pending(gid))

    press = interaction(boss, card)
    run(sanction.press(press, True))
    check("an administrator's press is what actually bans Sam",
          guild.banned == ["Sam"])
    check("the card becomes the receipt, naming who signed",
          "Approved by Boss" in card.content)
    check("and says what the hands reported back, not what was asked for",
          "Banned" in card.content and "Sam" in card.content)
    check("the buttons come off it", card.view is None)
    check("and the case book has it now, under the name of whoever asked",
          [c["moderator"] for c in warden.cases(gid, target_id=sam.id)] == ["Voter"])

    again = interaction(boss, card)
    run(sanction.press(again, True))
    check("a second press does not ban him twice",
          guild.banned == ["Sam"] and "already approved" in again.replies[0])

    # ----- denial -----
    run(powers.act_moderate(
        guild, voter, {"who": "Sam", "action": "warn", "reason": "again"}))
    card = cards()[-1]
    run(sanction.press(interaction(boss, card), False))
    check("a denied request says so and leaves no case behind",
          "Denied by Boss" in card.content
          and warden.cases(gid, target_id=sam.id, kind="warn") == [])

    # ----- lapsing -----
    run(powers.act_moderate(
        guild, voter, {"who": "Sam", "action": "kick", "reason": "enough"}))
    card = cards()[-1]
    rows = json.loads(settings.state_file(gid, sanction.PENDING).read_text())
    rows[-1]["expires_at"] = (datetime.now(timezone.utc)
                              - timedelta(minutes=1)).isoformat()
    settings.state_file(gid, sanction.PENDING).write_text(json.dumps(rows))
    check("the sweep lapses what nobody got to", run(sanction.sweep(guild)) == 1)
    check("the card says so rather than sitting there looking live",
          "lapsed" in card.content and "Nothing" in card.content)
    check("and lapsing is the end that does nothing", guild.kicked == [])
    stale = interaction(boss, card)
    run(sanction.press(stale, True))
    check("signing a lapsed request is refused, not honoured",
          guild.kicked == [] and "lapsed" in stale.replies[0])

    # ----- the half that does not wait -----
    before = len(cards())
    out = json.loads(run(powers.act_moderate(
        guild, voter, {"who": "Sam", "action": "untimeout"})))
    check("lifting a timeout is not made to wait for a signature",
          out.get("done") == "timeout lifted" and len(cards()) == before)

    # ----- rooms and messages -----
    run(powers.act_purge(guild, voter, {"channel": "health", "count": 5}))
    check("a sweep is filed too, and sweeps nothing until it is signed",
          desk.swept == 0 and "Delete 5 message(s)" in cards()[-1].content)
    run(sanction.press(interaction(boss, cards()[-1]), True))
    check("and then sweeps once", desk.swept == 1)

    run(powers.act_channel(guild, voter, {"channel": "health", "action": "lock"}))
    check("locking a room waits as well",
          "Lock #health" in cards()[-1].content)

    # ----- what is checked at the press, not at the asking -----
    run(powers.act_moderate(
        guild, voter, {"who": "Sam", "action": "ban", "reason": "later"}))
    card = cards()[-1]
    voter.coop = False  # removed from the roll while the card sat there
    guild.banned.clear()
    run(sanction.press(interaction(boss, card), True))
    check("a request outliving the standing of whoever asked is not carried out",
          guild.banned == [] and "no longer in the cooperative" in card.content)
    voter.coop = True

    # ----- refused at the asking, so no card is ever raised -----
    before = len(cards())
    out = json.loads(run(powers.act_moderate(
        guild, voter, {"who": "Nobody", "action": "ban"})))
    check("a request that could never work is refused on the spot",
          "goes by" in out.get("error", "") and len(cards()) == before)
    out = json.loads(run(powers.act_moderate(
        guild, voter, {"who": "Boss", "action": "kick"})))
    check("and the governance guard still bites before the desk sees it",
          "vote, not a word" in out.get("error", "")
          and len(cards()) == before)

    # ----- the house may switch it off -----
    warden.set_value(gid, "mod.require_signoff", False)
    guild.banned.clear()
    before = len(cards())
    out = json.loads(run(powers.act_moderate(
        guild, voter, {"who": "Sam", "action": "ban", "reason": "no gate"})))
    check("with the requirement off he acts on the word alone, as he used to",
          out.get("done") == "banned" and guild.banned == ["Sam"]
          and len(cards()) == before)
    warden.set_value(gid, "mod.require_signoff", True)

    # ----- the settings are the house's, and reachable by talking -----
    check("both switches are in the settings table under mod",
          {"mod.require_signoff", "mod.signoff_minutes"} <= set(warden.SPEC))
    check("and the sign-off store is per-server like everything else",
          settings.state_file(gid, sanction.PENDING).parent
          != settings.state_file(gid + 1, sanction.PENDING).parent)

    settings.configure(data)


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
    import brain, toolbox, warden  # noqa: F401

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
        ("Hadi", "@Eugene make me a blue role called sky"),
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
    member = types.SimpleNamespace(id=5, display_name="Hadi")
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

    check("the open floor is not the first thing he reads any more",
          "Buy a kettle" not in turn)
    check("it is a fact about the house instead, in the half he is told "
          "rather than asked", "Buy a kettle" in seen["system"][1])

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

    # ---- what he did, carried into the turns after it ----
    # The failure this exists for: three calls to the same listing tool in
    # ninety seconds, three different answers, each worked out from the
    # last wrong one because the results themselves were thrown away at the
    # end of every turn and only his prose about them survived.
    print("\nwhat the tools returned outlives the turn that called them")
    brain._memory.clear()
    brain._deeds.clear()
    brain._remember(99, "Hadi", "what colours are there")
    brain._note_deed(
        99, "Hadi", "list_color_roles", {},
        '{"roles_they_made": ["horsy role"], "roles_they_are_wearing": []}',
    )
    brain._remember(99, "Eugene", "You have one, and it is tomato red.")
    brain._remember(99, "Hadi", "make it purple")
    seen.clear()
    brain._call = fake_call
    try:
        run(brain._run_turn(guild, member, channel, "make it purple",
                            said_already=True))
    except RuntimeError:
        pass
    finally:
        brain._call = real_call
    later = seen["turns"][0]["text"]
    check("the next turn is handed the call itself, not his account of it",
          "list_color_roles" in later and '"horsy role"' in later)
    check("with the real spelling, which is the thing he kept inventing",
          "horsy role" in later)
    check("and told which of the two to believe when they disagree",
          "Where the two differ this one is right" in later)
    check("his own drifted summary is still there, and still only that",
          "tomato red" in later
          and later.index("list_color_roles") < later.index("tomato red"))
    check("a room where he has done nothing carries no such block",
          "already done in this room" not in brain._deed_log(12345))
    brain._deeds.clear()
    settings.configure(data)


def test_officer_gate(data):
    """The lock that does not read the model's output.

    brain.py already refuses a conversation to anyone outside the
    cooperative, so this gate should never fire in the ordinary run of
    things. It exists for the day something else calls dispatch.
    """
    print("\nthe elevated tools are the cooperative's alone")
    import warden  # noqa: F401  (registry is already loaded; kept explicit)

    check("the officer's tools are declared to the model",
          {"moderate_member", "set_setting", "purge_messages"}
          <= {d["name"] for d in toolbox.declarations()})
    check("and every one of them sits in the officer tier",
          all(toolbox.REGISTRY[name]["tier"] == "officer"
              for name in ("moderate_member", "purge_messages", "set_setting",
                           "assign_role", "announce")))
    check("while the ordinary ones did not quietly get promoted",
          toolbox.REGISTRY["propose_bill"]["tier"] == "member"
          and toolbox.REGISTRY["get_bill"]["tier"] == "minor")

    outsider = types.SimpleNamespace(id=1, display_name="Stranger")
    insider = types.SimpleNamespace(id=2, display_name="Hadi")

    toolbox.configure(HERE, data, in_cooperative=lambda m: m.id == 2)
    denied = json.loads(run(toolbox.dispatch(
        GUILD, outsider, "moderate_member", {"who": "someone", "action": "ban"})))
    check("someone outside is refused before a handler is even looked up",
          "not in it" in denied.get("error", ""))
    allowed = json.loads(run(toolbox.dispatch(
        GUILD, insider, "moderate_member", {"who": "someone", "action": "ban"})))
    check("someone inside gets through the gate",
          "not in it" not in allowed.get("error", ""))
    reading = json.loads(run(toolbox.dispatch(GUILD, outsider, "list_bills", {})))
    check("and the reading tools are not caught up in it",
          isinstance(reading, list))

    toolbox.configure(HERE, data, in_cooperative=None)
    shut = json.loads(run(toolbox.dispatch(
        GUILD, insider, "purge_messages", {"count": 50})))
    check("a host that never said who is on the roll fails shut, not open",
          "roll" in shut.get("error", ""))

    def broken(_member):
        raise RuntimeError("the roll is on fire")

    toolbox.configure(HERE, data, in_cooperative=broken)
    burnt = json.loads(run(toolbox.dispatch(
        GUILD, insider, "moderate_member", {"who": "x", "action": "kick"})))
    check("and a check that throws is a no, not a yes",
          "the answer is no" in burnt.get("error", ""))

    log_entries = json.loads((data / "logs" / "executor_log.json").read_text())
    refusals = [e for e in log_entries if e.get("result") == "denied"]
    check("every refusal is written down with its reason",
          len(refusals) >= 3 and all(e.get("detail") for e in refusals))

    toolbox.configure(HERE, data)  # back as the rest of the run expects it


def test_officer_guards(clerk, data):
    """What the hands refuse however the asking is phrased."""
    print("\nthe three things the hands will not do")
    import powers
    import warden

    settings.configure(data / "warden-store")

    class Role:
        def __init__(self, name, position, rid=None):
            self.name, self.position, self.id = name, position, rid or position
        def __ge__(self, other):
            return self.position >= other.position
        def __lt__(self, other):
            return self.position < other.position

    def person(pid, name, top=1, coop=False):
        return types.SimpleNamespace(
            id=pid, name=name.lower(), display_name=name, bot=False,
            top_role=Role("theirs", top), roles=[], guild=None, coop=coop,
        )

    keyed = lambda m: bool(getattr(m, "coop", False))  # noqa: E731
    powers.configure(None, keyed, None)

    sam, sammy = person(10, "Sam"), person(11, "Sammy")
    boss = person(12, "Boss", top=9)
    voter = person(13, "Voter", coop=True)
    me = person(99, "Eugene", top=5)
    guild = types.SimpleNamespace(
        id=6161, name="The Hangout", owner_id=1, me=me,
        members=[sam, sammy, boss, voter, me],
        get_member=lambda i: next((m for m in [sam, sammy, boss, voter] if m.id == i), None),
        get_channel=lambda _i: None, get_role=lambda _i: None, roles=[],
    )

    found, why = powers.find_member(guild, "Sam")
    check("an exact name wins over a longer one that starts the same",
          found is sam)
    found, why = powers.find_member(guild, "Sa")
    check("but an ambiguous scrap is reported, never guessed at",
          found is None and "could be any of" in why)
    found, _ = powers.find_member(guild, "<@11>")
    check("a mention is a person", found is sammy)
    found, why = powers.find_member(guild, "Nobody")
    check("and a stranger is said to be one", found is None and "goes by" in why)

    check("someone above Eugene in the role list is refused with the fix",
          "above me" in (powers._reachable(guild, boss) or ""))
    check("and Eugene will not act on himself",
          powers._reachable(guild, me) is not None)

    check("a member of the cooperative cannot be kicked on one person's word",
          "vote, not a word" in (powers._protected(guild, voter, "kick") or ""))
    check("nor banned", powers._protected(guild, voter, "ban") is not None)
    check("nor quietly silenced instead",
          powers._protected(guild, voter, "timeout") is not None)
    check("but they can still be warned, which decides nothing",
          powers._protected(guild, voter, "warn") is None)
    check("somebody who is not in it is ordinary business",
          powers._protected(guild, sam, "kick") is None)

    warden.set_value(guild.id, "mod.protect_cooperative", False)
    check("and the house can lift that protection, because it is theirs",
          powers._protected(guild, voter, "kick") is None)
    warden.reset(guild.id)

    check("nobody removes themselves through Eugene",
          "yourself" in (powers._vet(guild, voter, voter, "kick") or ""))


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
        test_people(data)
        test_one_memory_store(data)
        test_modules(data)
        test_slate(data)
        test_warden_settings(data)
        test_automod()
        test_cases(data)
        settings.configure(data)  # back to the shared store
        test_officer_gate(data)
        clerk = load_clerk(data)
        if clerk is None:
            skip("the debate thread", "discord.py is not installed")
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
            test_filing(clerk, data)
            test_setup_rooms(clerk, data)
            test_health_room_is_private(clerk, data)
            test_closing(clerk, data)
            test_close_floor_split(clerk, data)
            test_role_cap(clerk, data)
            test_colour_hands(clerk, data)
            test_voting(clerk, data)
            test_voting_numbers(data)
            test_open_polls(clerk, data)
            test_open_poll_closing(clerk, data)
            test_numbers_bite(clerk, data)
            test_choice_ballots(clerk, data)
            test_eligibility(clerk)
            test_duty_actions(clerk, data)
            test_chat_room(data)
            test_colour_words(clerk, data)
            test_colour_picking(clerk, data)
            test_dynamic_thresholds(data)
            test_empty_promises(data)
            test_prompt_caching(data)
            test_turn_shape(data)
            test_officer_guards(clerk, data)
            test_signoff(clerk, data)
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
