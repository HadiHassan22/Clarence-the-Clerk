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
    name="The Hangout", id=1,
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
    entries = json.loads((data / "executor_log.json").read_text())
    check(f"{len(entries)} entries written", len(entries) >= 10)
    check("proposals are logged with their arguments",
          any(e["tool"] == "propose_member" and e["args"].get("name") for e in entries))
    check("no dispatch is recorded without an outcome",
          all("result" in e for e in entries))


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
    cap = clerk.ROLE_CREATE_MAX
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

        clerk.ROLE_CREATE_MAX = 5
        check("were the cap ever raised, two of five is not at it",
              clerk.at_create_cap(99) is False)
        check("and the wording says the number plainly",
              clerk.role_cap_line() == "You already made 5 roles. "
                                       "One has to go to make room.")
    finally:
        clerk.ROLE_CREATE_MAX = cap
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
        guild = types.SimpleNamespace(id=gid, get_channel=lambda _cid: None)
        general = types.SimpleNamespace(id=10, parent_id=None)
        lounge = types.SimpleNamespace(id=20, parent_id=None)
        thread = types.SimpleNamespace(id=21, parent_id=20)

        check("bound to nothing, he talks anywhere, as he always did",
              brain.may_speak_in(guild, general) is True)
        bindings.bind_channel(gid, "chat", 20)
        check("bound, he talks in that room", brain.may_speak_in(guild, lounge) is True)
        check("and in no other", brain.may_speak_in(guild, general) is False)
        check("a thread hanging off it is still it",
              brain.may_speak_in(guild, thread) is True)
        bindings.bind_channel(gid, "chat", None)
        check("unbinding gives him the run of the place back",
              brain.may_speak_in(guild, general) is True)
    finally:
        settings.configure(data)


def test_study_gate(data):
    """Every reply used to buy a second model call asking what was worth
    remembering, and almost every answer was "nothing". The gate decides that
    for free. A false skip costs one fact somebody will say again; a false
    study costs money on every "thanks"."""
    print("\nwhat is not worth a second thought")
    import brain

    ok = "Noted. I will remember that for the next time it comes up."

    table = [
        # (label, said, reply, used_tools, the reason, or None to study it)
        ("thanks", "thanks", ok, (), "member said too little"),
        ("lol", "lol", ok, (), "member said too little"),
        ("ok", "ok", ok, (), "member said too little"),
        ("a padded nothing", "   ok   ", ok, (), "member said too little"),
        ("an allergy nobody should have to repeat",
         "I cannot eat dairy, so leave me off the pizza order next time",
         ok, (), None),
        ("a move worth filing",
         "I have moved to Berlin, so I am on CET now and not on GMT",
         "Right. I will read CET when I say what time a vote closes.", (), None),
        ("a question answered out of the registry",
         "what is open on the floor right now?",
         "Two proposals: No. 4 on the books channel and No. 5 on the rota.",
         ("show_bills",), "lookup"),
        ("the same question with nothing looked up",
         "what is open on the floor right now?",
         "Two proposals: No. 4 on the books channel and No. 5 on the rota.",
         (), None),
        ("a statement that happened to use a tool",
         "I have moved to Berlin, so I am on CET now and not on GMT",
         "Right. I will read CET when I say what time a vote closes.",
         ("show_bills",), None),
        ("the outage line", "what is on the floor at the moment?",
         brain.OUTAGE_LINE, (), "canned line"),
        ("the line he says when a question ran too deep",
         "what did everybody decide about the rota in the end?",
         brain.TOO_DEEP_LINE, (), "canned line"),
        ("a sealed ballot", "who voted against the books channel in the end?",
         "The ballots are sealed, so I cannot say who voted which way.",
         (), "refusal"),
        ("a short reply that says it cannot",
         "could you delete that channel for me please?",
         "I cannot do that without a proposal.", (), "reply too short"),
    ]
    for label, said, reply, used, want in table:
        got = brain._study_skip_reason(said, reply, used)
        check(f"{label}: {got or 'studied'}", got == want)

    print("\nand the floors it decides on")
    check("a message one character short of the floor is skipped",
          brain._study_skip_reason("a" * 24, ok, ()) == "member said too little")
    check("and one exactly at it is studied",
          brain._study_skip_reason("a" * 25, ok, ()) is None)
    check("a reply one character short is skipped",
          brain._study_skip_reason("a" * 25, "b" * 39, ()) == "reply too short")
    check("and one exactly at it is studied",
          brain._study_skip_reason("a" * 25, "b" * 40, ()) is None)
    long_refusal = "The ballots are sealed. " + "There is a reason for that. " * 6
    check(f"a long answer that merely contains the word is not a refusal "
          f"({len(long_refusal)} chars)",
          len(long_refusal) >= 160
          and brain._study_skip_reason("a" * 25, long_refusal, ()) is None)
    check("but one just inside the refusal length is",
          brain._study_skip_reason("a" * 25, "The ballots are sealed. " + "x" * 130,
                                   ()) == "refusal")


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

        brain._deps["role_limits"] = {"create": clerk.ROLE_CREATE_MAX,
                                      "wear": clerk.ROLE_WEAR_MAX}
        check("and what clerk.py enforces is what he tells people",
              brain._colour_limits() == "one colour of their own, five worn at once")
    finally:
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


def test_prompt_caching(data):
    """Eugene's character and the standing orders go out on every single
    request. They are only paid for once if the stable half of the prompt
    arrives byte-identical every time and the mark sits in the right place,
    so both are pinned here: a live value drifting into the cached half
    costs money silently, which is the one kind of breakage nothing else
    would catch."""
    print("\npaying for the standing orders once instead of every time")
    import brain
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
        toolbox.add_memory("fact", "Hadi", "allergic to shellfish",
                           source="observed")
        after = brain._system_prompt(guild)

        check("the stable half survives the memory book changing under it",
              before[0] == after[0])
        check("because the book is in the other half", "shellfish" in after[1])
        check("and never in this one", "shellfish" not in after[0])
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        check("today's date is kept out of the cached half",
              today not in after[0])
        check("the standing orders are inside it",
              "rules of procedure" in before[0])
        check("and the charter is gone from it entirely",
              "charter" not in before[0].lower())
        check("which together clear the annex's minimum several times over",
              len(before[0]) > 8000)
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
    finally:
        settings.configure(data)


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
        clerk = load_clerk(data)
        if clerk is None:
            skip("the filing handlers", "discord.py is not installed")
            skip("the chat room", "discord.py is not installed")
            skip("the study gate and the colour wording",
                 "discord.py is not installed")
            skip("dynamic thresholds", "discord.py is not installed")
            skip("prompt caching", "discord.py is not installed")
        else:
            test_filing(clerk, data)
            test_closing(clerk, data)
            test_close_floor_split(clerk, data)
            test_role_cap(clerk, data)
            test_voting(clerk, data)
            test_eligibility(clerk)
            test_duty_actions(clerk, data)
            test_chat_room(data)
            test_study_gate(data)
            test_colour_words(clerk, data)
            test_dynamic_thresholds(data)
            test_prompt_caching(data)
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
