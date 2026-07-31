# The Cooperative: design pass

Status: working draft. This is not law and nothing here is in force. It is a
gap analysis of the server model, with a recommended resolution for each gap
and an explicit list of what still needs deciding. Once it settles, it becomes
the charter and the rules of procedure, and the bot's persona is built on it.

Every section says what is undefined, why that bites, and what to do about it.

**Where things actually stand:** 8 people, all of them in the cooperative.
There are no non-cooperative members yet. So §1, §2 and §9 describe machinery
that is defined but dormant — worth pinning down now so the boundary is ready
when the first outside member arrives, but nothing to build or run today. The
roster is currently everybody.

**The speed requirement is a first-class constraint, not a nice-to-have.**
Decisions have to be able to land inside a live Discord conversation, in
minutes. Everything in §4 and §5 is built around that, and it is the reason the
thresholds count against the roster rather than against turnout: roster-counted
votes can close the instant the outcome stops being in doubt.

---

## 1. The two circles

The model has two circles and the boundary between them was never drawn.

**Members** are the server. They play, talk, propose things, vote in polls,
speak at gatherings. They have no management or moderation access.

**The Cooperative** is whoever has picked up a chore. Rotating owners, admins,
moderators. It is not a rank and not a promotion; it is the set of people who
agreed to do maintenance. Saying this out loud matters, because the entire
claim of the server is that there is no hierarchy, and a body called "staff"
with a private vote is exactly what a hierarchy looks like from outside. The
justification cannot be "they are more important." It has to be "someone has
to hold the keys, and these are the people who volunteered."

**How you become a member.** Any cooperative member may invite someone. The
invite is posted where the cooperative can see it and sits for 48h. If nobody
objects, they are in. One objection turns it into a Light vote. This is
deliberately the cheapest door in the system — your own note says the whole
point of splitting members from cooperative is to lower the threshold for
letting people in.

**How you join the cooperative.** Serious tier (see §4). You are being handed
keys and a ballot, so the bar is real. But it should be framed as taking on
work, not as being elevated.

**How you leave.** Instantly, unilaterally, no vote, no explanation, no notice
period. Step down to member whenever you want. If you cannot leave freely, the
role is a trap and the no-hierarchy claim is false.

**How you are removed from the cooperative.** Serious tier, with the subject
recused. Not the same as being removed from the server.

**Being removed from the cooperative is not a punishment and must never be
recorded as one.** Otherwise nobody will ever volunteer.

---

## 2. Who votes on what

Your draft has cooperative votes, and polls that include everyone, with
"making a poll requires 100% of cooperative votes."

**That rule is the worst one in the document.** It means a single cooperative
member can permanently prevent the community from being asked a question. In a
server whose stated value is community-first, *asking* should be the cheapest
action available, not the most expensive one in the entire system.

The fix is to separate asking from deciding:

- **Advisory poll** — any cooperative member opens one, alone, no threshold.
  It gathers opinion and binds nobody. Anyone can ask anything.
- **Binding poll** — the cooperative pre-commits to following the result.
  Opening one requires a cooperative vote at whatever tier the underlying
  decision would need. You are not gating the question, you are gating the
  promise.

A badly-framed advisory poll can be closed early by a simple majority of the
cooperative, with the reason posted. That handles abuse without handing anyone
a veto over curiosity.

**Which circle votes on what:**

| Question | Electorate |
|---|---|
| Rules, procedure, voting itself | Cooperative |
| Bans, removals, admissions | Cooperative |
| Cooperative membership, ownership pool | Cooperative |
| Safety and permission settings | Cooperative decides, admins execute (§7) |
| Channels, events, cosmetics, features | Cooperative, usually after a member poll |
| Anything at all, non-bindingly | Everyone |

---

## 3. The roster problem

Everything in your draft is a percentage of "available votes" and that phrase
is never defined. It is the single most important undefined term, because the
denominator decides whether the system works or deadlocks.

Counting share-of-everyone-on-the-list is the right instinct — it makes silence
count as a "no", which is what forces people to actually turn up and have an
opinion. But applied naively it means one person on holiday can sink a 2/3
vote, and a person who drifts away from Discord entirely can freeze the
cooperative forever without ever knowing it.

**Resolution: an active roster.**

- A cooperative member is on the roster by default.
- They can mark themselves **Away** at any time, with one command, no reason,
  no permission needed, no judgement attached. Away members are not in the
  denominator and their silence is not a no.
- The bot auto-marks someone Away after 14 days with no activity in the
  server, and tells them it did.
- Coming back, or unsetting it, puts you straight back on. No re-application.
- The roster size at the moment a vote opens is the denominator for that vote,
  and it is frozen there so the target cannot move mid-vote.

The window is short on purpose. Because thresholds count against the roster, an
absent person is an effective no — at 8 people needing 6 for a Fundamental
change, two people on holiday block everything. Away is what stops real life
from looking like opposition, so it has to trigger before an absence starts
silently vetoing things. 14 days is already generous at this size.

**Recusal.** The subject of a vote is removed from the denominator
automatically. Nobody votes on their own removal, their own ban, or their own
admission. This needs saying because with small numbers it changes outcomes.

---

## 4. Thresholds

The design goal you stated: trivial things should be easy, things that make
someone uncomfortable should be hard, and fundamental things should need more
than a majority.

The trap is reaching for 100%. Unanimity is not the safest setting, it is the
most dangerous one, for three reasons:

1. It gives every individual a permanent veto — precisely the dictator problem
   the server was built to avoid, just distributed.
2. It is self-locking. You cannot lower a 100% threshold, because lowering it
   requires 100%. Your manifesto says everything must stay open to question;
   a unanimity rule is the one thing that stops being questionable.
3. In practice it does not produce agreement, it produces silence — people
   stop proposing things they know one person will block.

A high supermajority of the *active roster* gets you everything you wanted from
unanimity, with none of that. It also, at this size, makes votes close in
seconds rather than days — see §5.

**The obvious fractions collapse at 8 people.** Two-thirds of 8 is 5.33 and
three-quarters of 8 is 6.0, and both round up to 6, so a "Serious" and a
"Fundamental" tier written that way are the same tier wearing two names. The
numbers below are picked so the tiers stay distinct at the size you actually
are, and stay distinct as you grow.

| Tier | Bar | At 8 | Covers |
|---|---|---|---|
| **Just do it** | no vote at all | — | topics, emoji, events, cosmetics, anything reversible in a minute |
| **Speak now** | silence is assent, 10 min | 0 clicks | anything reversible but worth flagging; one objection escalates it to Light |
| **Light** | majority of ballots cast, min 3 cast | 2 of 3 | channel names and settings, bots for internal testing, ban appeals, member admission on objection |
| **Serious** | 60% of roster | 5 of 8 | bans, joining or leaving the cooperative, the ownership pool, removing an owner |
| **Fundamental** | 75% of roster | 6 of 8 | rule changes, changes to voting itself, permanent bans, safety and permission settings |
| **Untouchable** | not amendable | — | the core value: everyone is respected |

Round up. How the two roster tiers scale:

| Roster | Serious (60%) | Fundamental (75%) |
|---|---|---|
| 6 | 4 | 5 |
| 8 | 5 | 6 |
| 10 | 6 | 8 |
| 12 | 8 | 9 |

The `min 3 cast` floor on Light exists so two people awake at 4am cannot decide
things alone. It is the only place quorum still needs stating — see §11.

The bottom row is how you get your one immutable thing without unanimity
maths. It is not protected by a number, it is out of scope for amendment
entirely. See §12 for the important caveat.

**Ban appeals sit at Light, not 100%, and this is deliberate.** Your draft had
exclusion at 67% and re-inclusion at 100%, which means exile is permanent by
default and decided by whoever is grumpiest that week. Make returning easy to
pass — and then protect the people it is meant to protect properly, with §4a
rather than with a number.

### 4a. The private safety block

Any single member — cooperative or not — may privately tell a moderator that
they do not feel safe with a specific person returning to the server. That
blocks the appeal. The person who raised it is never named, never has to
justify it in public, and never has to face the person they raised it about.
The appeal is simply recorded as not proceeding.

This does far more for a safe space than a 100% threshold does, because a
threshold forces the uncomfortable person to argue their discomfort in front of
everyone, which is the thing they were trying to avoid.

It can be abused, and the mitigation is that it is rare, it only ever blocks
re-entry rather than causing exclusion, and a moderator knows who raised it.

---

## 5. Speed: how a decision lands in five minutes

Your draft: a vote can be called after a discussion, quorum is 67% of the
cooperative "at least that many agree to vote."

Three problems. "Agreeing to vote" is a second election before the election,
and it is the step everything will die at. Quorum was never separated from
threshold, which are different things. And nothing in it can resolve inside a
conversation — the whole shape assumes days.

The way to make decisions fast is not to shorten the deadline. It is to make
most things not need a vote, and to let the votes that do happen end the moment
they stop being in doubt.

### 5.1 The vote ends when it is decided

This is the core mechanic and everything else hangs off it.

Because Serious and Fundamental count against the roster rather than against
turnout, the outcome becomes mathematically fixed part-way through. At 8 people
with Serious at 5:

- The 5th yes lands → it has passed. Nothing anyone does later can change that.
  **Close it immediately and announce.**
- The 4th no lands → only 4 yes votes remain possible, so 5 is unreachable.
  **It has failed. Close it immediately.**

Eight people in voice hitting buttons resolve a Serious vote in about forty
seconds, and no rule was bent to get there. The deadline stops being the thing
that decides when a vote ends and becomes a backstop for the case where people
have wandered off.

This also deletes quorum as a separate concept for these tiers: you cannot
reach 5-of-8 without 5 people having voted. Turnout is baked into the
threshold. Only Light, which counts ballots cast, still needs a floor.

### 5.2 Backstops

The deadline only matters when a vote does *not* resolve itself.

| Tier | Backstop | On expiry |
|---|---|---|
| Speak now | 10 min | passes |
| Light | 1 hour | fails if under 3 ballots, otherwise counts what it has |
| Serious | 12 hours | fails |
| Fundamental | 24 hours | fails |

A vote that expires fails as *not enough of us showed up*, which is recorded
distinctly from failing on the merits, and it can be re-opened immediately.

### 5.3 Quick vote

For something being decided live, the opener marks it a quick vote. The bot
pings the whole active roster at once, and the backstop drops to 5 minutes.

If it has not resolved in 5 minutes, it does **not** fail — it silently
converts to the tier's normal backstop and carries on. Trying to be fast should
never cost you the decision. This is the mechanism that makes voting feel
dynamic without making it unfair: the threshold never moves, only the clock.

### 5.4 Speak now

Most things should never reach a ballot. Anyone announces what they intend to
do, the bot posts it with a 10-minute timer, and silence is assent. A single
objection from anyone on the roster escalates it to a Light vote.

This is the largest speed lever in the design, and it is worth more than every
timing rule above combined — reducing the number of things that need voting
beats speeding up voting. Reserve it for things that are reversible; if undoing
it would be a whole thing, it belongs in a tier.

### 5.5 Discussion: gate on participation, not on the clock

Your draft required discussion before serious changes, which is right. A fixed
24h or 72h minimum is the wrong way to enforce it, because it blocks exactly
the case you care about — everyone already in voice, already talking it
through.

So gate on whether the conversation actually happened:

- **Serious** — a discussion thread must exist. No minimum duration.
- **Fundamental** — a thread must exist and either half the roster has posted
  in it, or 12 hours have passed, whichever comes first.

If 4 of 8 have spoken, the discussion has demonstrably happened and waiting
another 71 hours adds nothing. If nobody has spoken, the clock is what stops it
being rammed through at 3am.

### 5.6 Two ballot modes

Auto-closing is in tension with a fully secret ballot: if the bot closes the
instant the 5th yes lands, the timing itself tells you something. Resolve it by
splitting ballots by what they are about.

- **Counted ballot** — the default, for anything about *things*. Running tally
  is visible to the roster; identities never are. Auto-closes per §5.1. Fast,
  and in a group of 8 deciding a channel name, hiding the tally is ceremony.
- **Blind ballot** — mandatory for anything about a *person*: bans, removals,
  admissions, appeals, cooperative membership. No tally visible to anyone at
  any point. It closes when every non-recused roster member has voted, or at
  the backstop, and only then reveals the total. Closing on completeness leaks
  nothing.

The reason for secrecy is that you should not have to know your friend voted to
ban someone. That reason simply does not apply to renaming a channel. See §10.

### 5.7 Nudges

The bot DMs anyone who has not voted at the halfway point of the backstop, and
again near the end. For quick votes it pings the roster the moment the vote
opens.

This is what actually delivers "everyone has to give an opinion." A rule cannot
make people vote. A notification can.

---

## 6. The live incident

**This is the largest gap in the model and nothing in your draft addresses it.**

Someone is being racist in general chat at 3am. There is no vote fast enough.
If the answer is "wait 24h for discussion and then 2/3 of the roster", the safe
space is not safe, and the one immutable value in the server is the one thing
the procedure cannot protect.

**Resolution: immediate action is always allowed. It is permanence that needs a
vote.**

- Any moderator — or any cooperative member if no moderator is around — may act
  instantly: timeout, mute, delete, pull from voice, or remove with a reinvite.
- The action is provisional. It is posted publicly and immediately with a
  one-line reason, by the bot, automatically.
- It expires after 7 days unless a cooperative vote at the matching tier
  confirms it.
- The person acted on is told what happened and that it is provisional.

This gives you speed without giving anyone power. Nobody can permanently
exclude a person alone. Anybody can stop a bad night alone. And because every
such action is logged in public, misuse is visible to everyone immediately,
which is a far better check than a permission setting.

---

## 7. Admins: delay, not veto

Your draft says admins "do not override anyone in decisions" and then says
safety settings require "100% of Admin role votes only." Those contradict, and
with one admin the second one is a dictatorship over exactly the settings that
matter most.

**Resolution: split deciding from executing.** The cooperative decides. Admins
execute. Admins hold a consultation duty rather than a vote:

- Anything touching permissions, safety, bots, agents, or data must be reviewed
  by at least one admin before it goes live.
- The admin's job is to write down the risks, in public, in plain language.
  Their job is to explain, not to approve.
- If an admin genuinely believes something is unsafe, they can **stop the
  clock**: a 72h delay and a mandatory cooperative discussion. They cannot kill
  it. If the cooperative still wants it after hearing the risk, it happens.
- An admin can always refuse to personally perform an action they think is
  wrong. Nobody has to be the hands.

Delay-not-veto is the right primitive here. It makes expertise count without
making the technician a gatekeeper, which is what your own framing asks for.

Your bots-and-agents rule already had this shape — "1 admin consultation
required, can be tested internally, not public before consultation" — and it is
the best rule in the draft. This just generalises it.

---

## 8. Rotating ownership

Undefined in the draft: term length, how the pool is chosen, what handover
actually involves, and what happens when an owner goes dark or goes rogue.

- **Pool of 3**, rotating on a fixed term. 3 months is the obvious starting
  number; shorter makes handover friction dominate, longer starts to feel like
  an office.
- The pool is set and changed by **the cooperative** at Serious tier.
- **Removing an owner is a cooperative decision, not an owners' decision.**
  Your draft said "100% of rotating owners' votes", which means an owner who
  goes rogue can never be removed, because they simply vote no. That is the
  exact failure the entire design exists to prevent, reintroduced in one line.
- **The owner's duty is to not act.** Operationally: the owner takes no action
  that a cooperative decision has not authorised, except provisional action
  under §6. Every owner-level action the bot can see gets logged publicly and
  automatically.

**The honest part.** Discord requires one account to own the server, transfer
needs that account to act, and there is no mechanism that forces it. If an
outgoing owner refuses to hand over, no rule in this document can make them.
The real protection is not procedural: the structure, rules, registry, records
and decisions are backed up off-server, so the community can rebuild elsewhere
without whoever broke it. That is worth keeping from the old charter and worth
stating plainly rather than papering over.

Because the worst case is recoverable, trust here is cheap. Write the rules
accordingly.

---

## 9. What members actually get

The draft gives members polls, feedback at gatherings, and the ability to
propose features and events. The gap: nothing says a member proposal can ever
force the cooperative to actually respond. Without that, "community first" is
decorative and the suggestion box is where ideas go to die.

**Resolution:**

- A member proposal that picks up support from a handful of members, or from
  any two cooperative members, is **automatically tabled**. The cooperative
  must vote on it and must publish the outcome with reasons. They are free to
  vote it down. They are not free to ignore it.
- Any member can open an advisory poll of members with no gate at all.
- Any member can ask for something to be raised at the next gathering, and it
  goes on the agenda.

That is the difference between having a voice and having a suggestion box.

---

## 10. Privacy and transparency

Your draft: "votes remain private within the cooperative, no outside member
should be aware of who voted for what."

This is ambiguous between two very different systems, and the reason you gave
for the rule settles it. If the goal is that people vote without external
pressure, hiding ballots only from outsiders does not achieve it — in a group
of six, the pressure that actually matters comes from the other five. If you
can see that someone voted to ban your friend, that is exactly the pressure the
rule is trying to remove.

**Resolution: fully secret ballot.** Nobody sees an individual vote. Not other
cooperative members, not the owners, not the bot's operators. Only the count is
recorded, and only so votes can be changed while the vote is open.

**And then publish everything else, to everyone:** the question, the
discussion, the tally, the outcome, and what happens next. Members see all of
it. Transparency about *what was decided* and secrecy about *who voted how* are
not in tension; that pairing is the whole point.

One honest caveat: with three voters and a 3–0 result, secrecy is
mathematically zero. Nothing fixes that. Do not pretend otherwise.

---

## 11. Deadlock

Share-of-roster thresholds can deadlock the same way unanimity does, just more
slowly, so they need explicit escape hatches.

- **Ties fail.** Failing means the status quo holds, which is the conservative
  default and the right one.
- **Quorum is not a separate rule** anywhere except Light. Roster-counted
  thresholds contain their own turnout requirement, and adding a quorum check
  on top would only create a second way for the same vote to fail.
- **A failed proposal has a cooldown before it can be re-tabled**, unless it
  has materially changed: 1 hour for Light, 48 hours for Serious, 14 days for
  Fundamental. Small friend groups relitigate by exhaustion, and the person
  with the most stamina should not win by default. The cooldown scales with
  the tier so that being careful about rule changes never slows down deciding
  a channel name.
- **If a roster-counted vote expires for turnout twice in a row on the same
  question**, the third attempt drops to share-of-cast, with that fact recorded
  in the log. Otherwise absent members freeze the cooperative through inaction,
  which is the veto problem coming back through the side door.
- **If the cooperative drops below 3 active members**, everything except
  provisional safety action and admissions is suspended until it is back up.
  Two people are not a cooperative.

---

## 12. The immutable core, and its limits

The one unamendable thing: **everyone is respected, can discuss anything, and
respects everyone else.**

The gap: if the value is immutable *and* undefined, then in practice it means
whatever the loudest person in the room says it means, and an unamendable rule
is the most dangerous place for that to happen.

**Resolution: the value is immutable, its interpretation is not.**

- The value itself cannot be amended, repealed, or voted away.
- The rules that interpret it — the conduct rules, what counts as toxic, where
  the PG-13 line sits, how incidents are handled — are ordinary rules,
  amendable at Fundamental tier like anything else.
- Interpretation in a live case sits with whoever is moderating, provisionally,
  and is reviewable by the cooperative afterwards.

Separating the value from its interpretation is what stops it becoming a
weapon. Everything else in this document, including this document, is
questionable and changeable — that is §13.

---

## 13. Changing this document

Any part of this, once adopted, is amendable at Fundamental tier, including the
tiers themselves and including this section. The only exception is the core
value in §12.

A member who is not in the cooperative can propose an amendment through §9.

If most members raise a serious problem with how something is handled, that
tables a Fundamental discussion automatically. The cooperative does not get to
decide whether the complaint is worth hearing.

---

## 14. Meetings and notes

- **Cooperative meetings**, roughly monthly. Everyone gets a turn. No topics
  off the table, anyone can be criticised including whoever is running it. The
  point is to fix things, not to assign blame.
- **Server gatherings** — the roast-the-team format. Members say what they want
  and complain freely.
- **Notes are published within 72h or the meeting does not count as held.**
  Your draft made note-taking depend on who was available, which means it will
  quietly stop happening. Make it a condition instead. A member can take the
  notes; it does not have to be a cooperative member.
- Notes record what was said and what was decided. They do not record who
  voted how.

---

## 15. Where the bot sits

Worth stating explicitly, because it is unusual and it is load-bearing: the bot
is deliberately powerless. It has no vote and no opinion on anything open. It
cannot ban, kick, or rename. It cannot see individual ballots. It keeps the
record, runs the clock, nudges people who have not voted, publishes the notes,
and reports outcomes.

It is the timer and the notebook, not an authority. If it ever needs more power
than that to make the system work, the system is wrong.

---

## 16. Still open

Things I could not settle without you:

1. **The bot's new name and character.** The old one is a parliamentary clerk
   and does not survive this rewrite. Nothing gets written until this lands.
2. **Do moderators need to exist as a separate role at all**, or is moderating
   just a thing cooperative members do? The draft distinguishes them by
   temperament rather than by permission, and at 8 people where everyone is in
   the cooperative, that may not need a role — it may just need a rota for who
   is paying attention.
3. **The three owners.** Named pool, or elected each term? At 8 people a pool
   of 3 is more than a third of everyone, which is either fine or pointless
   depending on how you feel about it.
4. **Term length.** 3 months is a guess.
5. **Is 5 of 8 right for a ban?** It is the number most worth arguing about,
   because it is the one that decides whether someone stays. 6 of 8 is
   defensible too; 5 makes the room easier to protect, 6 makes it harder to
   throw someone out.
6. **The Speak-now window.** 10 minutes is fast enough to keep a conversation
   moving and short enough that someone asleep misses it entirely. That is a
   real trade and the number should be whatever you are comfortable with.
7. **§9's tabling threshold** — how many members it takes to force a vote.
   Dormant until there are members outside the cooperative, so this can wait.
