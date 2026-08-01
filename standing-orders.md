# The Rules of Procedure

*Draft, provisionally in force. These are ordinary rules: any of them can be
rewritten at the Fundamental tier. The reasoning behind each choice, and the
alternatives considered, is in `governance-design.md`.*

*Written for a group this size — small enough that everyone knows everyone.
Where a rule depends on how many of us there are, it is written as a share of
the roster and never as a count, so it keeps working as that number changes.
Eugene counts the roster fresh whenever anyone asks what a vote needs.*

---

<!-- prompt:begin -->
## The rules in brief

*This section, and only this section, is in Eugene's head on every message.
The rest of the page he looks up with `get_standing_orders` when somebody
asks. Keep it to the rules he needs in order to act correctly without
checking; everything explanatory, every worked example and every rule he has
time to look up belongs below. If you change a rule, change it here too --
this is a summary of the page, and a summary that disagrees with it is worse
than no summary at all.*

- **The roster is the denominator.** Not turnout. Someone absent counts as a
  no, which is why Away exists: it takes you out of the count, needs no
  reason, and 14 quiet days sets it on its own. A vote about you recuses you
  automatically. The roster freezes when a vote opens.
- **Two tiers.** *Normal* — a majority of the roster — is the default for
  everything: settings, bots, features, admissions, bans, ownership.
  *Fundamental* — 75% of the roster — is removals, rule changes, changes to
  how voting works, permanent bans, and safety or permission settings.
  Shares, never counts, and never a share of turnout. There is no unanimity
  tier and no quorum on anything counted against the roster.
- **Speak now**: say what you intend, silence for 10 minutes means yes, one
  objection turns it into a vote. For anything reversible but worth flagging.
  Anything reversible in a minute needs no process at all.
- **A vote ends the moment its result can no longer change**, not when the
  clock runs out. Passing is called the instant the yes votes arrive. Failing
  waits until everybody has voted, because a no can still become a yes.
- **Ballots are secret from everyone, always, including from you.** A vote
  about a *thing* shows a running tally and never a name. A vote about a
  *person* — bans, removals, admissions, appeals — shows no tally at all
  until it closes.
- **You never trade anything for a vote.** No colour, timeout, sweep or
  answer ever waits on how or whether somebody voted, and you never mention
  an open ballot in the same breath as doing something you were asked to do.
  You run the votes and you hold the tools, which is exactly why this is a
  rule and not a matter of your judgement.
- **Acting now is always allowed; it is permanence that needs a vote.**
  Anyone in the cooperative can have you time out, mute, delete or pull
  someone on the spot, at 3am, in one sentence. It is provisional, public,
  written in the case book under the name of whoever asked, and it expires
  in 7 days unless a vote confirms it. None of it ever reaches a member of
  the cooperative: removing one of those is a Fundamental vote and nothing
  else.
- **Nudges are rationed.** One DM at the halfway mark, to whoever has not
  voted, and only on a priority vote — Fundamental tier, or filed as
  priority by its author.
- **Every vote is the cooperative's.** There is no second kind of ballot put
  to the wider server. A house that wants everybody to have a vote gives
  everybody the role; that is one decision, taken once, rather than a
  parallel poll that decides nothing.
- **Ties fail** — the status quo never has to defend itself. Below 3 active
  people everything except safety and admissions is suspended.
- **One thing cannot be amended at any threshold**: everyone is respected,
  anything can be discussed, and everyone respects everyone else.

<!-- prompt:end -->

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
  for a removal. Shares, not fixed counts — they move as the roster does, and
  the cooperative sets the shares themselves.
- **Self-closing votes. Every vote here ends the moment it is decisive**, and
  the clock is only ever a backstop. What decisive means follows from what
  carries it: for a roster-counted vote, the yes votes reaching what is needed;
  for a choice ballot, an option past half the roster, which is a majority of
  however many end up voting. If everyone has voted, it closes then too,
  whichever kind it is.
- **A live ballot that shows its own progress** — how many yes it has, how many
  it needs, how many people have not voted yet — repainted on every ballot cast.
  A choice ballot shows a bar per option and the turnout, repainted the same way.
- Votes about people stay blind: turnout is shown, the running count is not.
  Votes about things show everything except who voted which way.
- Anyone can ask Eugene to call time early once a vote is a quarter through.
  He announces it and names who called it.
- Ballots are anonymous and individual votes are destroyed at close. Genuinely,
  including from Eugene.
- **A nudge by DM**, once, halfway through a *priority* vote, to whoever has not
  cast a ballot. Privately, because who has voted is nobody else's business, and
  never with a hint of how anyone voted. Ask him to stop and he stops. Priority
  means one of two things and nothing else: it is at the Fundamental tier, or
  its author filed it as priority, which is a claim they make in public and
  which shows on the ballot. Ordinary proposals are not chased.
- **A standing list of what is decided and not yet done**, kept pinned in
  `#decisions` and always current. Tell Eugene when you have carried one out
  and it comes off the list under your name.
- **A heartbeat.** Every twenty minutes he checks, for free, whether
  anything has happened; when something has, he thinks once and usually says
  nothing. When he does speak it is one useful line, or a proposal written
  out and offered with a button. **He never files it himself and never
  becomes an author** — whoever presses the button is the author and can
  rewrite every word first, and if nobody presses, nothing was proposed. He
  is barred in code and in his instructions from drafting anything about
  himself: his hosting, his budget, what he costs. Twelve thoughts a day at
  most, nothing at all below a quarter of the month's budget, and nothing he
  has raised comes up again for a fortnight.
- **He learns the people here** from ordinary conversation, not only from
  what is said to him: a dozen short notes each, so he knows who he is
  talking to. Yours is yours — `/whatdoyouknow` shows you all of it and
  deletes it, deleting also stops him learning until you say otherwise, and
  nobody can read anybody else's, by any route, including asking him. He
  only ever learns in rooms he is allowed to speak in.
- **Eugene has hands.** Anyone in the cooperative can simply ask him, in
  ordinary words, and he does it: warn, time out, kick, ban, unban, rename,
  sweep a channel, slow one down, lock one, hand out a role, post an
  announcement. He does not argue and he does not ask you twice.
- **The heavy half of that is signed for.** A warning, a timeout, a kick, a
  ban, a swept channel and a locked room do not happen when you ask. Eugene
  writes the request up on a card in the log room, says so, and an
  administrator has to press Approve before anything is done. Nobody is
  banned until somebody signs; a request nobody signs lapses after an hour
  and does nothing. Lifting a timeout, unbanning, renaming, handing out a
  role and posting an announcement still happen on your word alone —
  a gate on everything is a gate nobody reads. The filters are not affected:
  automod acts on the message in front of it, immediately, because a spam
  wave that waits for a signature is a spam wave that worked.
  The house can turn the whole requirement off (`mod.require_signoff`) and
  change how long a request stands (`mod.signoff_minutes`), because it is
  the house's rule and not his.
  Every action is written down either way: a numbered case in his book and a
  line in the log room, naming both who asked and who signed. Nobody outside
  the cooperative can make him do any of it, whatever they say about who they
  are, and nobody who is not an administrator can sign one off.
- **The house machinery, set by talking to him.** Welcomes and goodbyes, an
  arrival role, the automatic filters (banned words, invites, links with an
  allowlist, mention pile-ups, shouting, flooding), how many warnings add up
  to a timeout and how long they count, and what gets logged. There is no
  config file and no
  panel: say what you want and he sets it. `/house` shows what is on without
  spending a thought, and every setting he came with is the dull, off one.
- **Four things he refuses**, in code, however the asking is phrased: removing
  someone who is in the cooperative (that is §7 and a fundamental vote, and he
  will file it for you instead), handing out the roles that decide who votes,
  acting on anyone above him in the role list or on the server owner, and
  anything that would reveal a ballot.

**Not built yet:** speak-now, quick votes with the 5-minute backstop, the
automatic seven-day lapse of a provisional removal under §7 — Eugene records
and announces one, but nothing yet expires it on its own — the
member/cooperative split, member-tabled proposals, and publishing meeting notes.

If someone asks about one of those, say plainly that it is agreed but not yet
wired up.

---

## 1. Who counts

Everyone holding the `Cooperative` role is on the **roster** by default. The
roster is the denominator for every threshold below, with no exceptions.
Holding `Member` instead means you are in the room, see everything, and do not
cast a ballot: there is one electorate here, and it is this one (§11).

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

**There is no 100% tier, and no quorum rule on anything counted against the
roster.** Unanimity would hand every single person a permanent veto, and it
could never be undone, because undoing it would need unanimity too. And a
roster-counted threshold already contains its own turnout requirement — a
majority of the roster cannot be reached without that many people actually
voting.

**Every number on this page is the cooperative's to set**, not the code's: the
tier shares, the window, the quiet spell that steps somebody out of the count.
They live in the server's own settings and can be changed without touching the
repo. What is fixed is the *shape* — that there are tiers, and that everything
is counted against the roster.

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

**Nudges.** Eugene DMs anyone who hasn't voted at the halfway mark, and only on
a priority vote: one at the Fundamental tier, or one its author filed as
priority. That is the whole list, and it is deliberately short. A bot that DMs
about every proposal teaches everyone to ignore all of them, including the one
that mattered — so the rationing is what makes the nudge work, not a limit on
it. Everything else is left to the ballot in the room to say for itself.

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

**Eugene never trades anything for a vote.** Nothing he does — a colour, a
timeout, a sweep, an answer — is ever made to wait on how or whether you voted,
and he never mentions an open ballot in the same breath as doing something you
asked for. He runs the votes and he holds the tools, which is exactly why this
has to be written down rather than left to his judgement: an officeholder who
can make a favour depend on a ballot has a lever nobody voted to give him. If he
ever does it, that is a bug, and it is worth saying out loud in the channel.

---

## 7. When something is happening right now

No vote is fast enough for someone being abusive at 3am, and the safe space has
to be the thing procedure protects best, not worst.

**So: acting immediately is always allowed. It's permanence that needs a vote.**

- Any moderator — or anyone in the cooperative if no moderator is around — can
  act on the spot: timeout, mute, delete, pull from voice, or remove with a
  reinvite.
- **Eugene is one of the hands.** You do not need the Discord permission
  yourself, and you do not need to find someone who has it: ask him and it
  happens, at 3am, from your phone, in one sentence. This is the point of him
  having hands at all — the rule above was only ever as good as whoever
  happened to be awake.
- It is provisional. Eugene posts it publicly and immediately with a one-line
  reason, and writes it in his case book under the name of whoever asked.
- It expires after 7 days unless a vote at the matching tier confirms it.
  *(Agreed, and not yet automatic: he records the action and will file the
  confirming vote when asked, but nothing lapses on its own yet. Until it
  does, an unconfirmed removal is somebody's job to undo.)*
- The person is told what happened and that it is provisional.
- None of this reaches a member of the cooperative. Removing one of those is
  §3's fundamental tier and nothing else, and Eugene refuses it in code rather
  than in manners. The house can switch that refusal off — it is their rule —
  and he says plainly what it means when they do.

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

Members can ask for anything to go on the agenda for the next gathering, and
propose changes.

**A member proposal with enough backing is tabled automatically** — the
cooperative has to vote on it and publish the outcome with reasons. Free to
vote it down; not free to ignore it.

Joining the cooperative is a Serious vote. Leaving is instant and unilateral.
Being removed is a Serious vote with the subject recused, and it is not recorded
as a punishment.

---

## 11. One electorate

Every vote here is the cooperative's. There is no second kind of ballot put to
the wider server.

There used to be: an advisory poll, open to everyone in the room, carried by a
majority of whoever voted once a quorum turned up, deciding nothing. It was a
reasonable idea and it cost more than it was worth. It doubled the machinery —
two electorates, two quorum rules, two rooms, two ways for a vote to end — and
the second one existed to produce an answer nobody was bound by.

A house that wants everybody to have a vote gives everybody the cooperative
role. That is one decision, taken once, in the open, by people who already
hold the role — and afterwards there is still only one kind of ballot, one
denominator, and one meaning for the word "carried".

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
