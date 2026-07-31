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
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

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
    # asyncio.run, because get_event_loop stopped making one
    # of its own in 3.14 and the repo is developed on it
    return asyncio.run(coro)


GUILD = types.SimpleNamespace(name="The Hangout")
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

    print("\nthe floor cannot be buried")
    set_floor([{"no": i, "status": "on_floor", "author_id": 99} for i in range(2)])
    check("two open bills of your own is the limit", "error" in json.loads(
        run(toolbox.dispatch(GUILD, CITIZEN, "propose_bill",
                             {"title": "t", "what": "w", "why": "y"}))))
    check("and it applies to invitations too", "error" in json.loads(
        run(toolbox.dispatch(GUILD, CITIZEN, "propose_member",
                             {"name": "Sam", "why": "y"}))))
    set_floor([{"no": i, "status": "on_floor", "author_id": 1000 + i} for i in range(5)])
    check("a full floor stops everyone, not just the loudest", "error" in json.loads(
        run(toolbox.dispatch(GUILD, CITIZEN, "propose_bill",
                             {"title": "t", "what": "w", "why": "y"}))))
    set_floor([])
    check("the same caps bind the button, not only the asking",
          clerk._floor_full(CITIZEN) is None)
    set_floor([{"no": i, "status": "on_floor", "author_id": 99} for i in range(2)])
    check("a citizen at their limit is stopped whichever route they take",
          clerk._floor_full(CITIZEN) is not None)

    set_floor([{"no": i, "status": "passed", "author_id": 99} for i in range(9)])
    check("closed bills do not count against the cap", "filed" in json.loads(
        run(toolbox.dispatch(GUILD, CITIZEN, "propose_bill",
                             {"title": "t", "what": "w", "why": "y"}))))


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
                "kind": "ordinary", "author_id": 1, "notes": {},
                "ballots": {"1": "yes", "2": "yes", "3": "no"},
                "submitted_at": submitted.isoformat(),
                "ends_at": (submitted + clerk.timedelta(hours=window_hours)).isoformat()}

    live = {}
    clerk.bill_by = lambda field, value: live.get(value)
    closed = []

    async def fake_close(guild, bill):
        bill["status"] = "passed"
        bill["tally_line"] = "✅ 1 / ❌ 0"
        bill["act"] = 10
        closed.append(bill["no"])

    clerk.close_bill = fake_close
    clerk.post_closing_report = lambda guild, bill: _async(clerk.closing_report(bill))
    clerk.find_channel = lambda guild, needle: None
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

    print("\ncalling time cannot be aimed")
    thin = on_floor(hours_ago=13)
    thin["ballots"] = {"1": "yes"}
    live[5] = thin
    closed.clear()
    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "close_floor", {"bill_no": 5})))
    check("a floor nobody has voted on cannot be closed",
          "error" in out and not closed)
    check("and the refusal counts heads without reading them",
          "voted" in out.get("error", "") and "sealed" in out.get("error", ""))

    mine = on_floor(hours_ago=13)
    mine["author_id"] = CITIZEN.id
    live[5] = mine
    closed.clear()
    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "close_floor", {"bill_no": 5})))
    check("an author cannot call time on their own bill",
          "error" in out and not closed)

    for kind in ("invite", "kick"):
        people = on_floor(hours_ago=40)
        people["kind"] = kind
        live[5] = people
        closed.clear()
        out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "close_floor", {"bill_no": 5})))
        check(f"{'an' if kind == 'invite' else 'a'} {kind} bill is never closed early", "error" in out and not closed)

    live[5] = on_floor(hours_ago=13)
    live[5]["status"] = "passed"
    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "close_floor", {"bill_no": 5})))
    check("a bill that already closed cannot be closed again", "error" in out)

    check("an unknown bill number is refused", "error" in json.loads(
        run(toolbox.dispatch(GUILD, CITIZEN, "close_floor", {"bill_no": 999}))))
    check("a missing bill number is refused", "error" in json.loads(
        run(toolbox.dispatch(GUILD, CITIZEN, "close_floor", {}))))
    check("a non-numeric bill number is refused", "error" in json.loads(
        run(toolbox.dispatch(GUILD, CITIZEN, "close_floor", {"bill_no": "the books one"}))))

    # a three-minute sandbox floor must still be closable
    live[6] = dict(on_floor(hours_ago=0.02, window_hours=0.05), no=6)
    live[6]["ballots"] = {"1": "yes", "2": "yes", "3": "no"}
    clerk.bill_by = lambda field, value: live.get(value)
    out = json.loads(run(toolbox.dispatch(GUILD, CITIZEN, "close_floor", {"bill_no": 6})))
    check("the guard scales to a three-minute test floor", "error" not in out)


def _async(value):
    async def wrapper():
        return value
    return wrapper()


def main():
    data = Path(tempfile.mkdtemp(prefix="clerk-tests-"))
    try:
        toolbox.configure(HERE, data)
        test_registry()
        clerk = load_clerk(data)
        if clerk is None:
            skip("the filing handlers", "discord.py is not installed")
        else:
            test_filing(clerk, data)
            test_closing(clerk, data)
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
