# Making Eugene self-serve

Status: **§1, §2, §3, §4 and §8's door are built.** §5, §6 and §7 are not, and
§5 and §6 are deliberately parked — Eugene is hosted in one server, so isolating
data per guild buys nothing today. What remains is listed at the bottom.

Written after discovering that `server_config.yaml` describes a server nobody
actually has.

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

- `/setup rooms` — one select per job: where proposals are posted, where votes
  run, where decisions are recorded, where public polls go. Each stores an ID.
- `/setup roles` — a select for Cooperative and one for Member.
- `/setup status` — what is bound, what is missing, and what is switched off as
  a result.

Gate all of it on Discord's own `manage_guild` permission. Not a custom role,
not a hardcoded owner: the permission Discord already has for exactly this.

## 3. Dormant, not broken

The codebase already has the right precedent — the brain is dormant until
someone sets a key, and says so plainly. Extend that to everything.

A server that adds Eugene and configures nothing should still work for
conversation and colour roles. Each governance feature checks its binding and,
if it is missing, says *"no votes room is set; an admin can point me at one with
`/setup rooms`"*. Never a crash, never a silent no-op.

This also makes setup incremental. Bind the votes room today, polls next month.

## 4. Scaffolding that only ever adds

For a genuinely empty server, offer `/setup create`: it makes the channels and
roles that are missing, binds them, and stops. **It never renames, archives, or
deletes anything.**

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

## 7. Governance numbers become settings

Thresholds, tier shares, the backstop window, the auto-Away period and the
roster rules are constants and YAML today. Different servers will want
different numbers, and ours will want to change them without a deploy.

Per-guild, with the current values as defaults, behind `/setup voting`. The
tier *shape* stays code; the numbers become configuration.

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

---

## What is left

Done: binding by id, the setup wizard, dormant-not-broken, the sweep fenced off
behind a terminal flag, the signing door removed, the charter dropped entirely
(§8), the terminal build cut back to governance rooms only, and `/setup start`
— which is what stops a fresh server arriving with nobody in the cooperative
and no route in.

Parked while Eugene lives in one server: per-guild state (§5) and dropping
`GUILD_ID` (§6). Neither buys anything until he is in a second server, and both
are a lot of churn. When that day comes, §5 comes first — nothing else is safe
while two servers share a bills file.

Still worth doing here:

1. **Governance numbers as settings** (§7) — thresholds and windows are still
   constants. Changing them needs a deploy.
2. **The rules of procedure from the server** (§8) — `standing-orders.md` is
   still read out of the repo, and it is still ours rather than theirs.
3. **The unbuilt half of the design**: speak-now, quick votes, provisional
   moderation, public polls actually being openable. (DM nudges are built, and
   live in `duties.py`.)

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
- **How much opinion should the install carry?** The signing door and the charter
  are both gone, and the terminal build no longer creates a hangout nobody asked
  for. What is left is still an opinion about how a server should work; the test
  is whether it is one somebody can decline.
- **What happens when a bound channel is deleted?** Detect it, unbind, tell an
  admin. Otherwise the feature fails silently forever.
