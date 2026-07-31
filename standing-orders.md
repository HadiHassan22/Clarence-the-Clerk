# The Rules of Procedure

*Draft, provisionally in force. These are ordinary rules: any of them can be
rewritten at the Fundamental tier. The reasoning behind each choice, and the
alternatives considered, is in `governance-design.md`.*

*Written for a group this size — small enough that everyone knows everyone.
Where a rule depends on how many of us there are, it is written as a share of
the roster and never as a count, so it keeps working as that number changes.
Eugene counts the roster fresh whenever anyone asks what a vote needs.*

---

## 0. What is actually wired up right now

**Read this before telling anyone how a vote works.** Some of what follows is
agreed design that is not yet running code.

**Live now:**

- **Two roles.** `Cooperative` holds a vote. `Member` is in the room without
  one, and sits unused while all of us are in the cooperative. Neither is a
  rank, and neither sees more than the other: members and the cooperative see
  exactly the same rooms. The only difference is what the buttons let you do.
- **The rooms**: `#propose` to say what should change, `#votes` for what is
  open, `#decisions` for the permanent record. (Formerly `#submit-a-bill`,
  `#the-floor` and `#gazette`; the rename keeps their history.)
- The roster, and Away — set by giving yourself a role called `away`, or by
  going quiet for 14 days. Away takes you out of the denominator; being around
  puts you back. When the fortnight is what took you out, Eugene tells you so
  privately, and tells you again when you are back on.
- Thresholds counted against the roster: a **majority** for everything, **75%**
  for a removal. Shares, not fixed counts — they move as the roster does.
- **Self-closing votes.** The moment the yes votes reach what is needed, it
  passes and closes on the spot. If everyone has voted and it fell short, it
  closes then too. The 48-hour clock is only a backstop now.
- **A live ballot that shows its own progress** — how many yes it has, how many
  it needs, how many people have not voted yet — repainted on every ballot cast.
- Votes about people stay blind: turnout is shown, the running count is not.
  Votes about things show everything except who voted which way.
- Anyone can ask Eugene to call time early once a vote is a quarter through.
  He announces it and names who called it.
- Ballots are anonymous and individual votes are destroyed at close. Genuinely,
  including from Eugene.
- **A nudge by DM**, once, halfway through a vote, to whoever has not cast a
  ballot. Privately, because who has voted is nobody else's business, and never
  with a hint of how anyone voted. Ask him to stop and he stops.
- **A standing list of what is decided and not yet done**, kept pinned in
  `#decisions` and always current. Tell Eugene when you have carried one out
  and it comes off the list under your name.

**Not built yet:** speak-now, quick votes with the 5-minute backstop, logging
provisional moderation, the member/cooperative split, member-tabled proposals,
and publishing meeting notes.

If someone asks about one of those, say plainly that it is agreed but not yet
wired up.

---

## 1. Who counts

Everyone holding the `Cooperative` role is on the **roster** by default. The
roster is the denominator for every threshold below. Holding `Member` instead
means you are in the room, see everything, and do not cast a ballot.

**Away.** Anyone can mark themselves Away at any time. One command, no reason
needed, no permission needed, nobody thinks anything of it. Away people are not
counted in the denominator and their silence is not a no. Eugene marks someone
Away automatically after 14 days of no activity and tells them he did. Coming
back puts you straight back on.

This matters more than it looks: because thresholds count against the roster,
someone absent is effectively a no. Away is what stops real life from looking
like opposition.

**Recusal.** If a vote is about you — your ban, your removal, your admission —
you come out of the denominator automatically. Nobody votes on themselves.

**The roster is frozen when a vote opens**, so the target can never move
mid-vote.

**Below 3 active people**, everything except immediate safety action and
admissions is suspended until we are back up. Two people are not a cooperative.

---

## 2. Three speeds

Most things should never reach a ballot.

**Just do it.** Anything reversible in a minute — topics, emoji, events,
cosmetics. No process. Say what you did afterwards if it's worth saying.

**Speak now.** Say what you intend to do. Eugene posts it with a 10-minute
timer. Silence means yes. Any one objection turns it into a Light vote. Use
this for anything reversible but worth flagging.

**Vote.** Everything else, at the tier it belongs to.

---

## 3. The tiers

| Tier | Passes on | What it covers |
|---|---|---|
| **Speak now** | nobody objects in 10 min | reversible things worth flagging |
| **Normal** | a majority of the roster | the default for everything: channel names and settings, bots, features, admissions, bans, ownership |
| **Fundamental** | 75% of the roster | removals, rule changes, changes to how voting works, permanent bans, safety and permission settings |

Normal is a plain majority, because that is what people already expect a vote
to mean, and it keeps the everyday case easy. Round up, and a threshold never
asks for more than the roster holds.

**The thresholds are shares, not numbers.** The roster moves — people join,
leave, and step away — and what a vote needs moves with it, on its own, with
nothing to edit here. This page is deliberately free of worked examples for
that reason: a number written down is a number that goes stale, and someone
would end up quoting it a year later at a roster that had changed twice. Ask
Eugene what a vote needs today and he will count it.

**There is no 100% tier and no separate quorum rule.** Unanimity would hand
every single person a permanent veto, and it could never be undone, because
undoing it would need unanimity too. And a roster-counted threshold already
contains its own turnout requirement — a majority of the roster cannot be
reached without that many people actually voting.

**One thing is not on this table at all:** the core value, set out in §14.
Not amendable, at any threshold.

---

## 4. How a vote ends

**A vote ends the moment its result can no longer change.** Not when the clock
runs out.

The moment the yes votes reach what the tier needs, it has passed, and Eugene
closes and announces it on the spot. A room already in voice can settle
something in under a minute this way.

Failing is called differently, and deliberately. Eugene waits until everyone
has voted before calling a vote lost, because a no can still turn into a yes
while the ballot is open — so a threshold is only genuinely out of reach once
there is nobody left to change their mind.

While it runs, the ballot shows its own progress: how many yes it has, how many
it needs, and how many people still owe a vote.

The clock is only a backstop for when people have wandered off:

| Tier | Backstop | If it expires |
|---|---|---|
| Speak now | 10 min | passes |
| Light | 1 hour | fails under 3 ballots, otherwise counts what it has |
| Serious | 12 hours | fails |
| Fundamental | 24 hours | fails |

A vote that expires fails as *not enough of us showed up*, recorded separately
from failing on the merits, and can be reopened straight away.

**Quick vote.** If something is being settled live, flag it as a quick vote:
Eugene pings the whole roster and the backstop drops to 5 minutes. If it hasn't
resolved by then it does **not** fail — it quietly converts to the normal
backstop and carries on. Trying to be fast never costs you the decision. The
threshold never moves; only the clock does.

**Nudges.** Eugene DMs anyone who hasn't voted at the halfway mark and again
near the end. This is what makes "everyone gives an opinion" actually happen.

---

## 5. Discussion first

Anyone in the cooperative can call a discussion about anything, any time.

- **Serious** — a discussion thread has to exist. No minimum length.
- **Fundamental** — a thread has to exist, and either half the roster has
  posted in it or 12 hours have passed, whichever comes first.

Participation, not elapsed time. If four of us have already talked it through,
waiting three days adds nothing. If nobody has said a word, the clock is what
stops something being pushed through at 3am.

---

## 6. Ballots

Nobody ever finds out how anybody voted. Not other cooperative members, not the
owners, not whoever is running Eugene. Only counts are kept, and only so people
can change their vote while it's open.

**Counted ballot** — the default, for anything about *things*. Running tally is
visible; names never are. Closes as soon as the result is fixed.

**Blind ballot** — required for anything about a *person*: bans, removals,
admissions, appeals, cooperative membership. No tally visible to anyone at any
point. Closes when everyone not recused has voted, or at the backstop, and only
then shows the total.

The reason for secrecy is that you shouldn't have to know your friend voted to
ban someone. That reason doesn't apply to renaming a channel.

Everything that isn't the individual ballot is public, to everyone, including
members outside the cooperative: the question, the discussion, the tally, the
outcome, and what happens next.

With small numbers a lopsided result gives itself away. Nothing fixes that; we
just don't pretend otherwise.

---

## 7. When something is happening right now

No vote is fast enough for someone being abusive at 3am, and the safe space has
to be the thing procedure protects best, not worst.

**So: acting immediately is always allowed. It's permanence that needs a vote.**

- Any moderator — or anyone in the cooperative if no moderator is around — can
  act on the spot: timeout, mute, delete, pull from voice, or remove with a
  reinvite.
- It is provisional. Eugene posts it publicly and immediately with a one-line
  reason.
- It expires after 7 days unless a vote at the matching tier confirms it.
- The person is told what happened and that it is provisional.

Nobody can permanently exclude anyone on their own. Anyone can stop a bad night
on their own. Every such action is public, so misuse is obvious immediately.

---

## 8. Admins

The cooperative decides. Admins execute. Admins do not get a veto and do not
outrank anyone.

Anything touching permissions, safety, bots, agents or data gets reviewed by at
least one admin before it goes live. Their job is to write down the risks, in
public, in plain language — to explain, not to approve.

If an admin genuinely thinks something is unsafe they can **stop the clock**: a
72-hour delay and a mandatory discussion. They cannot kill it. If we still want
it after hearing the risk, it happens.

An admin can always decline to personally perform something they think is
wrong. Nobody has to be the hands.

Bots, tools and agents can be tested internally at any time, but nothing goes
public without an admin having looked at it.

---

## 9. Ownership

A pool of 3, rotating on a fixed term of 3 months.

The pool is set and changed by **the cooperative** at the Serious tier —
including removing someone from it. Owners do not get to decide who the owners
are; that would make a rogue owner unremovable, which is the exact thing this
whole arrangement exists to avoid.

The owner's duty is not to act. Concretely: no action that hasn't been decided,
except immediate safety action under §7. Eugene logs every owner-level action
he can see, publicly and automatically.

**The honest part.** Discord needs one account to hold the server and there is
no mechanism that forces a handover. If an outgoing owner refuses, no rule here
can make them. The protection isn't procedural — it's the off-server backup and
our willingness to rebuild elsewhere without whoever broke it. The structure,
the rules, the roster and the record are all backed up off the server; the only
thing genuinely at risk is message history, and we accept that.

---

## 10. Members

*Dormant: everyone is currently in the cooperative. This is here so the boundary
is ready when it's needed.*

Anyone in the cooperative can invite someone. The invite sits for 48 hours; no
objection and they're in, one objection makes it a Light vote. This door is
deliberately the cheapest in the system.

Members can open advisory polls with no gate at all, ask for anything to go on
the agenda for the next gathering, and propose changes.

**A member proposal with enough backing is tabled automatically** — the
cooperative has to vote on it and publish the outcome with reasons. Free to
vote it down; not free to ignore it.

Joining the cooperative is a Serious vote. Leaving is instant and unilateral.
Being removed is a Serious vote with the subject recused, and it is not recorded
as a punishment.

---

## 11. Polls

Any cooperative member can open an **advisory poll** alone, with no threshold.
It gathers opinion and binds nobody. Asking should be the cheapest thing in the
system, not the most expensive. A badly framed one can be pulled early by a
simple majority, with the reason posted.

A **binding poll** — where we commit in advance to following the result —
needs a vote at whatever tier the underlying decision would need. The gate is
on the promise, not on the question.

---

## 12. When things stall

- **Ties fail.** The status quo holds.
- **Cooldown before re-tabling** a failed proposal, unless it has materially
  changed: 1 hour for Light, 48 hours for Serious, 14 days for Fundamental.
  Nobody should win by having the most stamina.
- **If a vote expires for turnout twice on the same question**, the third
  attempt counts ballots cast instead of the roster, and Eugene records that it
  did. Otherwise people being absent freezes everything, which is the veto
  problem coming back in through the side door.

---

## 13. Meetings

- **Cooperative meetings**, roughly monthly. Everyone gets a turn. No topic off
  the table, anyone can be criticised including whoever is running it.
- **Server gatherings** — roast the team. Members say what they want, complain
  freely.
- **Notes get published within 72 hours or the meeting doesn't count as held.**
  Anyone can take them, including a member who isn't in the cooperative.
- Notes record what was said and what was decided. Never who voted how.

---

## 14. Changing these rules

Fundamental tier, including this section and including the tiers themselves.

If most members raise a serious problem with how something is handled, that
tables a Fundamental discussion automatically — the cooperative doesn't get to
decide whether the complaint is worth hearing.

The only thing outside all of this is the core value: *everyone is respected,
anything can be discussed, and everyone respects everyone else.* That cannot be
amended, repealed or voted away. How it gets interpreted — where exactly the
lines sit, how incidents get handled — is an ordinary rule and changes like
anything else. The value stays; its interpretation is ours to argue about.
