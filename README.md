# Eugene the Clerk

**A voting engine for a Discord server that governs itself.** Anyone on
the roll files a proposal — title, what, why. Eugene opens an anonymous
ballot, and it ends the moment its result can no longer change rather
than when a clock runs out. What passes becomes a numbered decision in a
permanent record, and he keeps a standing list of the ones nobody has
carried out yet.

Three things make that more than a poll bot:

- **The roster is the denominator, not turnout.** Not voting counts as a
  no, which is why stepping out of the count is one command and happens
  on its own after a quiet fortnight. Thresholds are shares of who is
  actually here, so they move as the roll does.
- **A vote ends when it is decided.** Passing is called the instant the
  yes votes arrive; failing waits for everyone, because a no can still
  become a yes.
- **Ballots are sealed from everybody, including from him.** They are
  destroyed at close. The tally survives; the votes do not.

**No AI key is needed for any of it, and with no key nothing leaves your
server.** Conversation is a separate feature, off out of the box, that a
house can switch on and pay for itself. See [PRIVACY.md](PRIVACY.md).

The rules: [standing-orders.md](standing-orders.md) — ordinary rules,
rewritable at the top tier, and the only document Eugene is given. One
server's charter, as an example of what a house might adopt on top of
them: [examples-charter.md](examples-charter.md).

## The programs

- **`clerk.py`**: the resident daemon. This is what gets hosted 24/7.
- **`modules.py`**: what he does here, in switchable parts. Each one
  declares the rooms it posts in, the roles it reads, the settings it
  owns and the tools it lends the model — which is simultaneously the
  switch, the structure preview, the build plan and the list the model is
  allowed to touch. Imports nothing from Discord.
- **`builder.py`**: the shaping itself — the rooms `/setup` builds, off
  the plan in `modules.py`.
- **`settings.py`**: per-server settings, keyed by guild id: the AI
  keys, the model, the budget.
- **`duties.py`**: the short list of things the clerk says without being
  asked, and the ledger that stops him saying any of them twice. Costs
  nothing: no line of it consults the model.
- **`powers.py`**: the small hands. Which member somebody meant, and the
  two tools that configure the clerk himself.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env               # then fill in DISCORD_TOKEN
.venv/bin/python clerk.py          # run the clerk
```

Then, in your server: **`/setup`**. One command, one screen. It reads the
server and says what is done and what is missing:

```
## Eugene in Book Club
1 of 3 features are running. Press Apply to finish the rest.

✅ 1 · Roles      — @Cooperative · @Member
⬜ 2 · Rooms      — missing `votes`, `decisions` — Apply makes them
       -# Channels: make new ones. Channels changes it.
⬜ 3 · The cooperative — empty. Nobody can propose or vote
✅ 4 · Features   — 1 on, 1 off
⬜ 5 · Brain      — optional. Governance runs without one, and with no
                   key nothing leaves this server
⬜ 6 · This place — he describes it neutrally rather than guessing

[ Features ] [ Rooms ] [ Roles & votes ] [ Brain ] [ Numbers ]
[ Channels ] [ Preview the structure ] [ Apply ] [ What this place is ]
[ Invitation message ]
```

**Nothing in your server changes until you press Apply**, and **Preview
the structure** prints the layout that is about to exist before it
exists — every room marked as one he will create, one he will adopt
exactly as it stands, or one already bound, with the feature that asked
for it named beside it.

**Channels** is the one decision he will not make for you: whether Apply
builds his own rooms or takes over ones you already have. Building is
the default, because adopting used to happen automatically from a name
match — a `#votes` you made for something else quietly became the floor.
Set it to adopt and the panel names the exact channels it would pick up
before you press anything. **Rooms** points any job at any channel by
hand, whatever it is called, which is the way to use one you already
have under a different name.

Apply makes the `Cooperative` and `Member` roles, binds them, creates the
channels your switched-on features want and do not have, and — the part
that matters — **puts you in the cooperative**. Until somebody is in it,
Eugene refuses everyone, including whoever installed him.

After that, **Roles & votes** hands the cooperative to anyone else who
should have one, and that is the only way onto that roll: it is a chore
somebody passes to you, not a ballot. `/invite` is a different door
entirely — it is a vote about somebody who is not in the server yet, and
what it hands out is a link.

That door is the one thing here with a last word attached. An invitation
that passes carries a **veto** button on its decision in `#decisions` for
a day, and one veto from anybody who could have voted takes it back: the
link dies, and whoever came through it goes back out. Pressing it asks to
be confirmed before anything is cast. It is the only kind of proposal that
starts with one, because it is the only one whose cost lands on everybody
and whose benefit is usually one person's. **Numbers** on the panel
switches it off, extends it to every proposal, prices the two separately,
moves the window, and decides whether a veto is named on the decision or
cast anonymously.

What that door hands out arrives as a private message to whoever
proposed them, and **Invitation message** on the panel is where a house
writes it in its own words. His default names both of them — *Robin: the
cooperative has approved your invitation of Sam (Proposal No. 12). One
link, single use, seven days: …* — and a house can put `{name}`,
`{invitee}`, `{proposer}`, `{server}`, `{number}` and `{link}` wherever
it likes. Anything else in braces is left on the page as typed rather
than swallowed, and a message that leaves `{link}` out still gets the
link on the end: the sentence is the house's, but a congratulation
nobody can use is not a thing he will send. Leave it blank for his.

**Nothing here names a server.** Eugene reads the name off Discord, so
renaming the server is the whole of it — proposal text, his own
description of himself and the health card all follow. What he cannot
read off Discord is what the place is *for*, so **What this place is**
takes one line of it: *a book club that argues about endings*. Leave it
unset and he describes the server neutrally rather than guessing.

Everything Apply does is additive. It never renames, moves, re-topics,
re-permissions or deletes a channel you already had, and it only makes
rooms a feature you left switched on actually asked for — no hangout, no
memes channel, nothing you did not ask for.

The bot needs Administrator with its role at the top of the role list,
and the **Server Members** and **Message Content** privileged intents
enabled in the Developer Portal. He keeps every server he is invited to,
each with its own record, roster, settings and bill. If you cannot enable Message Content,
set `CLERK_MESSAGE_CONTENT=0`; everything works except talking.

## What members can type

The same governance the buttons give you, without hunting for the button.
Every one of these does its work directly — no model is consulted and
nothing is spent, where asking him for the same thing in a channel costs
a conversation:

- **`/propose`** — file a proposal: title, what, why. Add `priority: True`
  and everyone who has not voted gets one DM halfway through; leave it off
  and nobody is chased.
- **`/invite`** — propose that someone outside be invited into the server.
  What passes is a single-use link, sent privately to whoever proposed
  them; it is not a seat in the cooperative and not a vote.
- **`/remove`** — propose that someone be removed. Says what the
  instrument costs before it asks who.
- **`/close <number>`** — call time on a vote that has had its run.
- **`/bills`** — what is open for a vote right now.
- **`/house`** — every feature and whether it is running, and the numbers
  this house votes by. Read-only, and free; the changing is done by asking
  him.
- **`/privacy`** — what leaves this server, where it goes, and how much of
  the room he is holding right now. Anyone can run it, and there is a
  button on it that makes him forget what he is holding.
- **`/access`** — which channels he can see, which he listens in, and
  which are invisible to him. Worked out from Discord's permissions, not
  from a list somebody wrote down.

They are the cooperative's, and refuse anyone else. A command belonging to
a feature the server has switched off says so, and names who can switch it
back on. `/setup` is the administrators', and so is one command:

- **`/model`** — which Claude does the talking: `haiku`, `sonnet` or
  `opus`. Run it bare and he says where he is and what else there is; give
  it a rung and he checks the server's own key can reach that model before
  he moves. The monthly counter re-prices itself to match, so an opus month
  is billed as one.

## What he does here, in two parts

Every feature is a module with a switch, and **Features** on the `/setup`
panel is the whole list, ticked or unticked.

| Feature | What it is | Out of the box |
|---|---|---|
| Governance | proposals, anonymous ballots, the permanent record | **on** |
| Conversation | he answers when mentioned, and does as he is asked | off |

Conversation is off because it is the only part of him that needs an AI
key and the only part that sends anything outside your server. It used to
be on and dormant, which meant a fresh install showed a feature waiting on
something and read as half-broken — when in fact the whole voting engine
was running and nothing was missing.

Switching one off is not cosmetic. Its commands say it is off and name
who can turn it back on, its rooms stop being built, its settings are
marked as read by nothing, and **it disappears from the tool list the
model is handed** — so a feature you switched off cannot be talked into
existence by asking him nicely for it.

The panel distinguishes four states throughout:
🟢 running · 🟡 on but waiting on a room or a key · 🟠 needs another
feature that is off · ⬜ off.

`/house` prints the same list, and the numbers this house votes by, to
anyone in the cooperative — free, and without consulting the model.

## Starting again

Half of what the clerk remembers is kept per server, in `guilds/<id>/`, and
comes out clean for a new one: settings, keys, warnings, cases. The other
half — the record, the roster, what he remembers, the ledgers of what he
has said — sits at the top of the data directory from the days when there was
only ever one house, and follows the daemon anywhere it goes.

So the directory is **stamped with the server it belongs to**. Point the
clerk at a different one and he says so, in the log and in `#bot-health`,
rather than turning up carrying the last server's proposals and its
numbering. He never clears it on his own: a wrong `GUILD_ID` is a typo
somebody fixes in a minute, and a parliament's record deleted because of
one is not recoverable.

Clearing it is **`/setup` → Start fresh**, by scope, nothing ticked to
begin with, and the button opens a box you have to type `ERASE` into after
reading what each scope costs:

| Scope | What goes |
|---|---|
| The record | every proposal and decision; **numbering starts again at 1** |
| What he has said | the ledgers that stop him repeating himself |
| The roster | last-seen times |
| The case book | warnings, cases, saved answers |
| His own posts | the message ids of what he keeps pinned |
| The install | bindings, feature switches, voting numbers, the house line |

Two things it never does. **It never touches an AI key** — that is the one
thing in the store that costs money to replace and cannot be read back off
Discord. And **it never deletes anything in Discord**: no channel, no role,
no message.

## Giving the clerk a brain

The clerk keeps records and holds the door with no AI at all. To make him
talk, someone with the keys to the server sets one from **Brain** on the
`/setup` panel: Gemini is a key from Google AI Studio, Grok from
console.x.ai, Claude from console.anthropic.com.

Any one will do, or several; the same screen switches which one speaks and
forgets one again. Keys are typed into a private modal, checked against the
API before they are saved, and kept in the server's own settings file at
`0600`; only the last four digits are ever shown or logged. Each server
pays for its own thinking and has its own monthly budget, spend counter and
rate limits.

Setting one wakes Conversation. **Details** says what is configured.
`/setup` is restricted to administrators.

## The numbers the server votes by

Thresholds, windows and caps are the server's, not the repo's. **Numbers**
on the `/setup` panel prints all eight and says which ones this server
chose rather than inherited; picking one opens a box with its current value
and its bounds, and `default` puts it back. One at a time on purpose — a
form that changes six of them at once is a form somebody will change six of
them at once with.

| Number | What it decides | Default |
|---|---|---|
| `floor_hours` | how long an ordinary vote stays open if nothing settles it | 48 |
| `removal_hours` | the same, for a removal | 72 |
| `fundamental_share` | the share of the roster a removal or rule change needs | 0.75 |
| `kick_min_yes` | the fewest yes votes a removal can ever pass on | 3 |
| `away_days` | a quiet spell this long takes you out of the count | 14 |

Each is held inside a range where the rest of the machinery still means
what it says, so a share cannot be set above everybody and a window cannot
be set to nothing. Nothing is cached: a number changed at noon is the one
he quotes at one minute past, including on votes already open. Votes
already on the floor keep the window they were filed with.

## One kind of vote

Every ballot is the cooperative's — `/propose`, `/invite`, `/remove` — and
every one is counted against the roster, so not voting is a no and it passes
the moment the yes votes reach what is needed. A ballot with options is
carried by whichever option gets past half the roster.

There is no second kind put to the wider server. A house that wants everyone
to have a vote gives everyone the `Cooperative` role; that is one decision,
taken once, by people who already hold it — and afterwards there is still one
electorate, one denominator, and one meaning for "carried".

**Nobody is DMed except about a priority vote**, which means one at the
fundamental tier (a removal, a rule change) or one whose author filed it as
priority with `/propose priority: True`. Ordinary proposals aren't chased — a
bot that DMs about everything teaches people to ignore the one that
mattered.

**A proposal is one message and stays one message.** `#votes` gets a single
card when it is filed — the proposal, its live ballot and its buttons — and
that card is edited from then on: on every vote, when a runoff reopens it,
and when it closes. Nothing is posted under it, ever. The argument happens
in the thread hanging off it, and what the vote came to goes to
`#decisions`, which likewise keeps one message per proposal and edits it as
the window opens, the report lands and, if it is taken back, the decision
is struck. Filing used to cost four messages and closing another four; a
room you have to scroll is a room where the open vote is the thing you
miss.

Each annex has a default model, chosen to be cheap rather than clever —
the clerk answers in a sentence or two, and a frontier model is several
times the price for a job that size. The modal's second field overrules
it for that server: put `claude-opus-5` or `gemini-3.1-pro` there and you
get a better writer at a higher bill. The spend counter prices the
defaults, so a server that changes model and cares about the figure
should set `price_in_per_m` and `price_out_per_m` in its settings file to
match. Claude answers with thinking off, which is what keeps a
one-sentence reply to one sentence.

He answers in `#eugene-chat`, which he makes himself under **Apply** and
binds to the `chat` job — there, in its threads, and in direct messages,
and nowhere else. Point `chat` at a different channel under **Rooms** if
you would rather he lived somewhere you already have. Anyone who mentions
him elsewhere gets one short pointer to the right room, at most once an
hour, and it costs nothing to say.

Unbind `chat` entirely and he falls back to the rooms he was given: the
governance category and anything you have pointed at a job. He does not
get the run of the whole server that way — the only server he answers
everywhere in is one nobody has set up at all, where he has no rooms of
his own to be kept to.

## What he says without being asked

Three things, none of which consult the model, so none of them cost
anything:

- **A nudge**, by DM, once, halfway through a vote, to whoever has not
  cast a ballot — because thresholds count against the roster, so
  forgetting to vote is the same as voting no. Never in public, never a
  hint at how anyone voted. Ask him to stop and he stops.
- **A word when the roster lets you go.** Fourteen quiet days takes you
  out of the denominator; the standing orders promise he tells you he
  did, and now he does, on both edges.
- **A standing list of decisions that passed and have not happened**,
  pinned in the `decisions` room and always current. Tell him one is
  carried out and it comes off under your name. Once a week, if the list
  is not empty, one line points at it.

Everything he starts is written into `duties.json` before it is said, so
nothing is ever said twice — including things that could not be
delivered, because a DM that bounces is not one to keep retrying.

## Installing it on someone else's server

Each person runs their own copy: their own Discord application, their own
`install.py`, their own AI key. Nothing is shared between installs, so a
friend can break, rebuild or reshape their server without touching yours.
Point them at the four steps under **Setup**.

The single-server assumption still lives in `GUILD_ID`: one daemon keeps
one house. Settings, keys and brain accounting are already stored per
guild, so the remaining work to serve several houses from one process is
scoping the rest of the state (`bills.json`, `acts.json` and friends) the
same way, and replacing the three functions under `# which house` in
`clerk.py`.

## Hosting

The clerk is a long-running worker, not a web service. It needs exactly
two things from a host: the two environment variables, and a **disk that
survives deploys**. Everything the server has ever decided lives on that
disk, along with its AI keys; without one, every redeploy is a fresh
start with an empty gazette.

Required environment: `DISCORD_TOKEN`, `GUILD_ID`, and `CLERK_DATA_DIR`
pointing at the mounted disk. Optional: `CLERK_MESSAGE_CONTENT=0` to run
without a brain, `CLERK_BUDGET_USD` to change the default monthly cap.

The deploy announcement in `#bot-health` fires when the running commit
changes. `RAILWAY_GIT_COMMIT_SHA`, `RENDER_GIT_COMMIT` and a few others
are picked up automatically; set `CLERK_COMMIT` yourself on a host that
provides none.

### Railway

`railway.json` in this repo sets the start command, so a fresh service
needs no build configuration. Deploy from the GitHub repo, then:

1. **Variables**: `DISCORD_TOKEN` and `CLERK_DATA_DIR=/data`.
2. **Volume**: add one to the service, mount path `/data`. Do this before
   the first real use, not after.
3. Redeploy, and watch the logs for `on duty as ...`.

Railway only sets `PORT` if the service has a domain. With one, the
health endpoint answers on `/healthz`; without one, it simply does not
bind, which is the correct behaviour for a worker.

### Render

A **Background Worker** (no HTTP port), start command `python clerk.py`,
`PYTHON_VERSION=3.13`, and a persistent disk mounted where
`CLERK_DATA_DIR` points.

### Keys after a move

`install.py` writes AI keys next to the code; a host reads them from
`CLERK_DATA_DIR`. After the first deploy, either set the key again from
**Brain** on the `/setup` panel (simplest) or copy `guilds/` onto the
disk.

State files are runtime data and are gitignored on purpose. So are `logs/`
(the harness's audit log) and `raw/` (anything imported from elsewhere,
such as a channel export — which is everybody's messages and ids, and is
not something to publish). So is
`guilds/`, which holds the keys. Never commit it.

## The voice he ships with

Read this before you invite him, because it is the default and not a
blank slate.

Eugene is written as a **comrade rather than a functionary**: warm,
plain, a bit irreverent, on your side by default, and he will call people
comrade when it lands. He is on the left and says so when it comes up
instead of hiding behind being a machine. He supports Palestinian
liberation and opposes the Israeli state's occupation, its blockade of
Gaza and its killing of Palestinian civilians, and he will say that
plainly if somebody asks rather than both-sidesing it.

That is a position about a state and what it does. It is aimed at no
people: never at Jewish people, never at Israelis as people, and he
pushes back if somebody tries to take it there.

**None of it reaches a ballot.** He has no vote and no opinion on how any
open question should be decided, he never hints at one, and he does not
bring politics into rooms that have nothing to do with it. That fence is
in the prompt and pinned by a test, because it is the whole reason a
house lets him run its votes.

If you want a different clerk, `/setup` → **What this place is** has a
second box for how this house wants him to sound. Whatever you write
there outranks the default. It changes how he talks and nothing else: the
ballot arithmetic, the sealed votes and the refusals are code.

## Privacy

[PRIVACY.md](PRIVACY.md) says exactly what leaves your server, with the
line of code that does it. The short version: **with no AI key, nothing
leaves** — every vote, the record, the roster and the reminders are plain
Python on your own host. With a key, one request goes out when somebody in
the cooperative speaks to him, carrying that room's recent conversation and
nothing about anybody who is not in it. He does not read rooms he cannot
answer in, he keeps no notes on people, and nothing is ever sent on a timer.

An administrator has to read a summary of that and accept it before a key
can be set, and who accepted is recorded.

## License

MIT — see [LICENSE](LICENSE).
