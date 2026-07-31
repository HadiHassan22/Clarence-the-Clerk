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
    return asyncio.get_event_loop().run_until_complete(coro)


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
    set_floor([{"no": i, "status": "passed", "author_id": 99} for i in range(9)])
    check("closed bills do not count against the cap", "filed" in json.loads(
        run(toolbox.dispatch(GUILD, CITIZEN, "propose_bill",
                             {"title": "t", "what": "w", "why": "y"}))))


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
