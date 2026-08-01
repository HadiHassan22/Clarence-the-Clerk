# Making Eugene self-serve

Status: **§1, §2, §3, §4, §9 and §8's door are built.** §5, §6 and §7 are not,
and §5 and §6 are deliberately parked — Eugene is hosted in one server, so
isolating data per guild buys nothing today. What remains is listed at the
bottom.

Written after discovering that `server_config.yaml` describes a server nobody
actually has. §9 was added after discovering the sequel: that twelve `/setup`
subcommands describe an install nobody can follow.

---

## The inversion

Today the repo holds a blueprint and the bot reshapes a server to match it.
`build_server.py` sweeps: every text channel not in the config is archived,
every voice channel not in it is deleted. That only works in one situation —
you own the server, it is empty, and you are the person who wrote the config.

Self-serve is the opposite. **The server is the source of truth and the bot
binds to what is already there.** Every change below follows from that one
inversion.

---

## 1. Bind by ID, not by name

This is the keystone; do it before anything else.

Right now a channel is found by name: `find_channel(guild, "the-floor")`, with
emoji stripping, base-name matching, a substring fallback, an alias table for
renames, and a `former:` list in the config so a rename adopts the old channel.
That is five mechanisms compensating for one bad decision.

Store **channel IDs and role IDs** in per-guild settings instead. Then:

- renames are free, because Discord IDs never change
- emoji prefixes stop mattering
- `CHANNEL_ALIASES`, `base_name()`, `former_bases()` and the rename-adoption
  logic can all be deleted
- "my server is laid out differently" stops being a problem by construction

A server's binding is just:

```json
{
  "channels": {"proposals": 123, "votes": 456, "decisions": 789, "polls": 321},
  "roles":    {"cooperative": 111, "member": 222}
}
```

Nothing else in the design needs to know what anything is called.

## 2. Setup is a binding wizard, not a build

Discord has native `ChannelSelect` and `RoleSelect` components. Nobody should
be typing channel names.

**Built, then rebuilt.** The first version of this was one subcommand per job —
`rooms`, `roles`, `status`, and by the end nine more — which solved the typing
and created a worse problem: twelve doors, no order between them, and three of
them able to make channels. See §9.

What it is now: `/setup`, one command, one stateful ephemeral panel. Selects for
every binding, a preview of the layout before anything is built, and a single
Apply. Gated on administrator, checked again in every callback rather than once
when the panel opened.

## 3. Dormant, not broken

The codebase already has the right precedent — the brain is dormant until
someone sets a key, and says so plainly. Extend that to everything.

A server that adds Eugene and configures nothing should still work for
conversation and colour roles. Each feature checks its binding and, if it is
missing, says *"governance is switched on but not running yet: no `votes` room
is bound"*. Never a crash, never a silent no-op.

This also makes setup incremental. Bind the votes room today, polls next month.

Generalised in §9 into four states rather than two: **on**, **dormant** (on,
missing something), **blocked** (on, standing on something that is off), and
**off** (somebody decided). The last of those is the one the original design
missed, and conflating it with dormant is how a bot ends up silently not doing
what it was told.

## 4. Scaffolding that only ever adds

For a genuinely empty server, Apply makes the channels and roles that are
missing, binds them, and stops. **It never renames, archives, or deletes
anything.** Which channels those are is not a fixed list: it is whatever the
switched-on features asked for, so a server that wants no parliament is not
handed a votes room (§9).

The existing `sweep()` is a founder's tool for greenfield, and in a self-serve
bot it is a loaded gun pointed at somebody's server. Either cut it from the
shipped path or put it behind an explicit, unmistakable confirmation that spells
out exactly which channels will be archived and which voice channels destroyed.
It must never be reachable from a command someone might run to "set things up".

## 5. Per-guild state

Everything currently at the data root is shared across servers:
`bills.json`, `acts.json`, `roster.json`, `signatures.json`, `clerk_state.json`,
`roles.json`, the memory book, the executor log.

`settings.py` already has the pattern, including one-time adoption of
single-server files:

```python
settings.state_file(guild_id, "bills.json", legacy_root=DATA)
```

Move everything onto it. Existing history gets adopted into the first guild
rather than abandoned. Proposal numbering becomes per-guild too — every server
starts at No. 1.

Do this first. Nothing else is safe to ship while two servers share a bills file.

## 6. Drop single-tenancy

`GUILD_ID`, `home_guild()` and `serves()` assume one server. They need to go:

- background loops iterate every guild the bot is in, not one
- slash commands sync globally instead of to a single guild
- `resolve_guild()` for DMs picks among the servers the speaker shares with
  Eugene, and asks which one if there is more than one

## 7. Governance numbers become settings — *built*

Thresholds, tier shares, the backstop window, the auto-Away period and the
roster rules were constants and YAML. Different servers will want different
numbers, and ours will want to change them without a deploy.

Now per-guild, with the old values as defaults, behind `/setup voting`. The
tier *shape* stays code; the numbers are configuration. Eight of them:
`floor_hours`, `removal_hours`, `fundamental_share`, `public_quorum_share`,
`kick_min_yes`, `away_days`, `role_create_max`, `role_wear_max`.

Three things that were worth getting right:

- **Every number is clamped to a range where the rest of the machinery still
  means what it says.** A share above 1 is a threshold nobody can meet; a
  window of 0 is a vote that closes before anyone sees it. `settings.py` holds
  the bounds and a value outside them is held at the nearest one, never stored.
- **Nothing caches them.** `numbers(guild)` is read fresh on every ballot,
  every prompt, every refusal. A number read once at boot is a number Eugene
  will eventually quote after it stopped being true, and he will do it with
  complete confidence.
- **`/setup` is the steward's door, and that is a stopgap.** Thresholds are
  emphatically not an administrator's business — but a proposal has no hands.
  When the executor lands, a passed decision comes through `settings.set_voting`
  and the steward stops being the only way in. Until then the honest description
  is that the numbers left the repo and have not yet reached the parliament.

**Public polls, end to end.** `audience` used to decide who the buttons let in
and nothing else — an open poll was still counted against the cooperative's
roster, a poll of sixty passing or failing on a roster of eight — and nothing
could file one anyway. Now:

- The denominator and the door are the same function (`belongs_to`), so a
  ballot can never be counted against people it would turn away.
- `floor_for()` puts an open poll in the `polls` room and the cooperative's
  business on the floor, and the argument about it happens in the thread on the
  vote itself, so it inherits whatever the room says rather than carrying a
  second set of permissions that could disagree with the ballot's. A vote
  posted where the people it is open to cannot read it is not open.
- `/poll`, a button in the proposals room, and an `open_poll` tool so it can be
  asked for in conversation. All three refuse cleanly when no `polls` room is
  bound, and say how to bind one.
- It is **advisory**: no numbered decision, not on the record, and the closing
  report says so whichever way it went. Acting on one is a separate proposal.

**Every vote ends early when it is decisive**, including polls. The test
differs because the rule differs: a threshold reached for a roster-counted
vote, an option past half the roster for a choice ballot, and for a poll a lead
bigger than the number of people left to vote with the quorum met. Same idea
three times — the point past which nobody left can change the answer.

**Nudges are rationed to priority votes.** They were sent for every cooperative
vote, which is the fastest way to teach a server to ignore a bot. Now: the
fundamental tier, or a bill its author filed as priority — a claim made in
public, shown on the ballot, available from `/propose priority: True` and from
the `propose_bill` tool. Polls are never nudged, and `priority` on a poll is
ignored rather than honoured.

## 8. The charter has to come from the server — *settled: it does not come at all*

`get_charter` read `constitution.md` out of the repo, which was our charter and
nobody else's. Every escape route from that — bind a charter channel, read its
pinned message, ship a template — kept a document at the centre of a bot whose
job is running votes.

So the charter is gone: the tool, the room binding, the posting step and the
prompt section. Eugene is given the rules of procedure and nothing else. A
server that wants a founding document writes one in a channel and never has to
tell him about it.

The rules of procedure are still half document and half code behaviour, and are
still shipped from the repo. Letting a server replace them with their own is the
part of this that remains open.

## 9. Features are modules, and the switches are the structure

The sequel to §2. Binding by id fixed *where* things are; it left two questions
with no answer inside Discord: what is he actually doing in my server, and how
do I stop him doing one of them. `pulse` had no switch at all, `automod` had one
buried in a table of forty settings, and the colour roles had one only in the
sense that you could decline to make a channel called roles.

So every feature is a module in `modules.py`, and each one declares, in one
place:

- the rooms it posts in, and which of them it cannot run without
- the roles it reads
- the `warden.SPEC` groups it owns
- the tools it lends the model
- the modules it stands on

That single declaration does five jobs. It is the switch. It is the structure
preview, generated from what is on, so it cannot describe a server other than
the one Apply produces. It is the build plan, for both `/setup` and
`build_server.py`, which is what finally kills the duplicate description of the
governance rooms that `server_config.yaml` was holding. It is the tool list the
model is handed, so a switched-off feature cannot be talked into existence. And
it is how `/house` knows which feature reads a setting.

Two rules make it hold:

- **Off is off, and dormant is not off.** Four states, said out loud, never
  silence.
- **A dependency is not a suggestion.** `memory` and `pulse` need `chat`;
  `enabled()` is the only function that gets to answer whether something runs,
  and it follows the chain rather than trusting each call site to remember.

Every default is what the clerk already did before there were modules, so an
upgrade in place is not a change of behaviour and every switch is a real switch.

---

## What is left

Done: binding by id, the setup wizard, dormant-not-broken, the sweep fenced off
behind a terminal flag, the signing door removed, the charter dropped entirely
(§8), the terminal build cut back to what the features ask for, features split
into switchable modules (§9), and one `/setup` that ends with somebody in the
cooperative — which is what stops a fresh server arriving with nobody in it and
no route in.

Parked while Eugene lives in one server: per-guild state (§5) and dropping
`GUILD_ID` (§6). Neither buys anything until he is in a second server, and both
are a lot of churn. When that day comes, §5 comes first — nothing else is safe
while two servers share a bills file.

Still worth doing here:

1. **The rules of procedure from the server** (§8) — `standing-orders.md` is
   still read out of the repo, and it is still ours rather than theirs. It has
   also just grown a section describing behaviour, which makes the gap worse:
   a server that wants different rules now has a document that is wrong about
   them in more places.
2. **The rest of the unbuilt design**: speak-now, quick votes, provisional
   moderation. (DM nudges are built, and live in `duties.py`.)
4. **The numbers reaching the parliament rather than the steward** (§7 above) —
   a passed decision should be able to set one without an administrator.

---

## What this does not change

The work already done is structure-independent and survives intact:

- the two roles and what each may do
- `may_vote()`, which reads the proposal rather than the channel, so it does not
  care how a server is laid out
- the roster, the thresholds, and votes that close when they are settled
- the live ballot display
- the persona

Only the *binding* layer is wrong. The governance layer is not.

---

## Open questions

- **Hosting and cost.** Each server already brings its own brain key, which is
  the right model — hosting stays cheap because thinking is paid for by whoever
  is using it. Worth keeping that property deliberately.
- **One bot or many?** A single hosted instance is easier for users and puts the
  token and everyone's data in one place. Self-hosting per server is more work
  for them and keeps their data theirs. This decides how carefully step 5 has to
  be done.
- **How much opinion should the install carry?** Mostly answered by §9: the
  opinion is now a set of defaults, every one of them declinable in one screen,
  and declining one is a real off switch rather than an empty room. What is left
  of the opinion is the defaults themselves, and the honest test of those is
  whether a server that unticks half of them still gets a bot that makes sense.
- **What happens when a bound channel is deleted?** Answered: `bindings.prune`
  drops the dangling id, the feature reads as dormant rather than silent, and
  the panel names the gap.
