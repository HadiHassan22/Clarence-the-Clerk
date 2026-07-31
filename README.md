# Eugene the Clerk

The executive of a Discord server run as an experiment in direct
self-governance. No moderators, no admins; a parliament of everyone, and
this bot as the only officeholder.

How it works, in one breath: anyone in the cooperative files a proposal
(title, what, why), Eugene opens an anonymous ballot, and a vote ends the
moment its result can no longer change rather than when a clock runs out;
what passes becomes a numbered decision in the permanent record, and
Eugene keeps a standing list of the ones nobody has carried out yet.
Members also get self-service colour roles.

The rules: [standing-orders.md](standing-orders.md) — ordinary rules,
rewritable at the top tier, and the only document Eugene is given.
Design history and scopes: [governance-design.md](governance-design.md),
[ROADMAP.md](ROADMAP.md), [voting-system-scope.md](voting-system-scope.md),
[roles-scope.md](roles-scope.md). Brand kit: [branding/](branding/).

## The programs

- **`clerk.py`**: the resident daemon. This is what gets hosted 24/7.
- **`modules.py`**: what he does here, in twelve switchable parts. Each
  one declares the rooms it posts in, the roles it reads, the settings it
  owns, the tools it lends the model and the modules it stands on — which
  is simultaneously the switch, the structure preview, the build plan and
  the list the model is allowed to touch. Imports nothing from Discord.
- **`install.py`**: the first-run wizard. Asks four questions, writes
  `.env`, stores any AI keys, prints your invite link.
- **`build_server.py`**: the structure builder, from a terminal. Builds
  exactly what `/setup` would — the rooms your switched-on features ask
  for — because both read the same plan out of `modules.py`. `--list`
  prints it without building; `--full-layout` adds the hangout from
  [server_config.yaml](server_config.yaml), which only suits an empty
  server. Idempotent.
- **`builder.py`**: the shaping itself, shared by `build_server.py` and
  `/setup` so both do exactly the same thing.
- **`settings.py`**: per-server settings, keyed by guild id: the AI
  keys, the model, the budget.
- **`survey.py`**: the long look. Every rule that decides what is broken,
  what is not what the house thinks it is, what wants tidying and what has
  never been set up. Discord-free, so the whole audit runs against a
  made-up server on a laptop.
- **`slate.py`**: whose history is on this disk, and how to clear it.
  Stamps the data directory with the server it belongs to, says so out
  loud when that stops matching, and knows what each kind of forgetting
  costs. Imports nothing from Discord.
- **`duties.py`**: the short list of things the clerk says without being
  asked, and the ledger that stops him saying any of them twice. Costs
  nothing: no line of it consults the model.
- **`pulse.py`**: the heartbeat. When he is worth waking, and what he may
  do awake. This is the one proactive thing that spends, so the whole
  module is the fence around it — the gate, the daily cap, the budget
  floor, and the ledger of what he has already raised.
- **`people.py`**: who he knows. Short notes on each person, owned by that
  person, readable and deletable by them and by nobody else.
- **`warden.py`**: the house rules he keeps without a vote — every
  setting a conversation can change, with its type and its bounds, plus
  the filters, the level curve and the case book. Imports nothing from
  Discord, so all of it is testable on a laptop.
- **`powers.py`**: the hands. What `warden.py` decides, this presses:
  timeouts, bans, sweeps, roles, welcomes, the log.
- **`sanction.py`**: the sign-off desk. The heavy half of those hands —
  warns, timeouts, kicks, bans, sweeps, channel changes — is asked for
  here and carried out only once an administrator has put their name to
  the card. Holds the pending requests, the two buttons, and the rule
  that a request nobody signs does nothing at all.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python install.py        # writes .env, prints your invite link
.venv/bin/python clerk.py          # run the clerk
```

Then, in your server: **`/setup`**. One command, one screen. It reads the
server and says what is done and what is missing:

```
## Eugene in Book Club
4 of 12 features are running. Press Apply to finish the rest.

✅ 1 · Roles      — @Cooperative · @Member
⬜ 2 · Rooms      — missing `polls`, `health` — Apply makes them
⬜ 3 · The cooperative — empty. Nobody can propose, vote or talk to him
✅ 4 · Features   — 9 on, 3 off · waiting on something: conversation, memory
⬜ 5 · Brain      — no key, so he keeps records and says nothing
⬜ 6 · This place — he describes it neutrally rather than guessing

[ Features ] [ Rooms ] [ Roles & votes ] [ Brain ] [ Numbers ]
[ Preview the structure ] [ Apply ] [ What this place is ] [ Details ]
```

**Nothing in your server changes until you press Apply**, and **Preview
the structure** prints the layout that is about to exist before it
exists — every room marked as one he will create, one he will adopt
exactly as it stands, or one already bound, with the feature that asked
for it named beside it.

Apply makes the `Cooperative` and `Member` roles, binds them, creates the
channels your switched-on features want and do not have, and — the part
that matters — **puts you in the cooperative**. Until somebody is in it,
Eugene refuses everyone, including whoever installed him.

After that, **Roles & votes** hands the cooperative to anyone else who
should have one. Once there are a few of you, the ordinary way in is
`/invite`, which is a vote.

**Nothing here names a server.** Eugene reads the name off Discord, so
renaming the server is the whole of it — proposal text, his own
description of himself and the health card all follow. What he cannot
read off Discord is what the place is *for*, so **What this place is**
takes one line of it: *a book club that argues about endings*. Leave it
unset and he describes the server neutrally rather than guessing.

Everything Apply does is additive. It never renames, moves, re-topics,
re-permissions or deletes a channel you already had, and it only makes
rooms a feature you left switched on actually asked for — no hangout, no
memes channel, nothing you did not ask for. The full starting layout
exists for an empty server and has to be asked for by name
(`build_server.py --full-layout`).

The bot needs Administrator with its role at the top of the role list,
and the **Server Members** and **Message Content** privileged intents
enabled in the Developer Portal. If you cannot enable Message Content,
set `CLERK_MESSAGE_CONTENT=0`; everything works except talking.

## What members can type

The same governance the buttons give you, without hunting for the button.
Every one of these does its work directly — no model is consulted and
nothing is spent, where asking him for the same thing in a channel costs
a conversation:

- **`/propose`** — file a proposal: title, what, why. Add `priority: True`
  and everyone who has not voted gets one DM halfway through; leave it off
  and nobody is chased.
- **`/poll`** — ask the whole server a question instead of the
  cooperative. Yes/no, or up to ten options.
- **`/invite`** — propose that someone be let in.
- **`/remove`** — propose that someone be removed. Says what the
  instrument costs before it asks who.
- **`/close <number>`** — call time on a vote that has had its run.
- **`/bills`** — what is open for a vote right now. The cooperative sees
  everything; anyone else in the room sees the open polls.
- **`/role`** — make or manage your colour.
- **`/whatdoyouknow`** — everything he has picked up about you, and
  `forget: True` to delete the lot. Yours alone; nobody can run it on
  anybody else.
- **`/survey`** — what is broken here, what is missing, what wants
  tidying. Free, and the same list whether or not the server has a key.
  `deep: True` has him read it and say which of it actually matters.
- **`/house`** — every feature and whether it is running, then the
  settings underneath them: welcomes, filters, warnings, the log.
  Read-only, and free; the changing is done by asking him.

They are the cooperative's, and refuse anyone else. A command belonging to
a feature the server has switched off says so, and names who can switch it
back on. `/setup` is the administrators', and so is one command:

- **`/model`** — which Claude does the talking: `haiku`, `sonnet` or
  `opus`. Run it bare and he says where he is and what else there is; give
  it a rung and he checks the server's own key can reach that model before
  he moves. The monthly counter re-prices itself to match, so an opus month
  is billed as one. The long look (`/survey deep: True`) keeps its own
  model and is not touched.

## What he does here, in ten parts

Every feature is a module with a switch, and **Features** on the `/setup`
panel is the whole list, ticked or unticked. A server can run him as a
parliament with no moderation, as a moderator with no parliament, or as
either with nothing else at all.

| Feature | What it is | Out of the box |
|---|---|---|
| Governance | proposals, anonymous ballots, the permanent record | on |
| Polls | advisory questions put to the whole server | on |
| Colour roles | self-service colours, yours or anyone's | on |
| Conversation | he answers when mentioned, and does as he is asked | on¹ |
| Memory of people | short notes on the people here, each owned by them | on¹ |
| Heartbeat | he notices things unprompted; the only feature that spends on its own | on¹ |
| Moderation | the filters, warnings, and the hands | off |
| Arrivals | greetings, goodbyes, an arrival role | on, until pointed at a room |
| Audit log | deletes, edits, arrivals, every moderation action | on |
| Health card | his vitals, pinned and current | on |

¹ *needs an AI key, so it is on and dormant until somebody sets one.*

**Nothing greets anybody until you say where.** Arrivals is on out of the
box and does nothing at all until the `welcome` job is pointed at a
channel, because most servers already have something for this — Discord's
own join notices, or a bot they had before this one — and a second hello
in a room nobody chose is worse than no hello. `/setup` → Apply binds a
channel you already have if one is obviously it, and **never creates one**.
If Discord is also greeting people, `/survey` says so and names both rooms;
which one to keep is yours.

Every default is what the clerk already did before there were modules, so
upgrading changes nothing and each switch above is a real switch rather
than a quiet change of behaviour.

Switching one off is not cosmetic. Its commands say it is off and name
who can turn it back on, its rooms stop being built, its settings are
marked as read by nothing, and **it disappears from the tool list the
model is handed** — so a feature you switched off cannot be talked into
existence by asking him nicely for it.

The switch above is the *only* switch. There is no second
`automod.enabled` underneath it: what lives in the settings is how a
feature behaves once it is on (what a link costs, how many warnings add up
to a timeout, what the greeting says), never whether it runs. You can also
just say it — *turn the filters on*, *stop greeting people* — which is the
same switch from the other side.

Three of them stand on `Conversation`, because a brain he may not have is
what they are made of: switch that off and memory and the heartbeat go
with it, and the panel says so rather than leaving two switches that look
on and do nothing. The panel distinguishes four states throughout:
🟢 running · 🟡 on but waiting on a room or a key · 🟠 needs another
feature that is off · ⬜ off.

`/house` prints the same list to anyone in the cooperative, free, without
consulting the model.

## The long look

Give him the run of the server and ask what needs doing. `/survey`, or the
**What needs doing** button on the setup panel, or just say it — *what
needs cleaning*, *what have we not finished*, *is anything wrong here*.

He walks the whole place once and grades what he finds:

| | |
|---|---|
| 🔴 **broken** | cannot work until somebody acts — a missing permission, a role above his, a bound room he is not allowed to post in, a feature switched on and waiting |
| 🟠 **wrong** | works, but not the way the house thinks — two channels claiming the same job with the binding on the empty one, a colour register that has drifted, a vote past its window and still open |
| 🧹 **untidy** | works, is correct, and there is cruft — empty categories, rooms nobody has used in two months, a full archive |
| ⬜ **missing** | nothing is wrong, something has simply never been set up — an optional room, a cooperative of one, decisions passed and not carried out |

**All of the looking is free.** Every rule is plain Python over facts
already in memory: no model, no tokens, no network. A server with no key
gets exactly the same list. A feature somebody deliberately switched off is
never a finding — that was a decision, and an audit that grades every
choice a fault is one people learn to close.

### Opus mode

`/survey deep: True` is the other half, and the only thing in the clerk
that deliberately reaches for an expensive model. The free list is
exhaustive rather than considered — nineteen true things in no meaningful
order — and turning that into *do these three, ignore the rest, here is
why* is judgement over a lot of context at once, which is the job cheap
models are worst at.

So each key carries two models. The **Brain** screen has a field for each:

```
Model for talking      claude-haiku-4-5     ← a mention, answered in a sentence
Model for the long look claude-opus-5       ← twenty findings, answered with judgement
```

Both have sensible defaults per annex (`gemini-3.1-pro`, `grok-4`,
`claude-opus-5`), so it works without anybody setting anything, and either
can be overruled. Add a question and he answers that first:

```
/survey deep: True question: what needs cleaning
```

It is one call over a small prompt — the findings, the question, and
nothing else: no transcript, no house book, no persona. That is what keeps
a frontier model on it worth a few cents rather than a line on the bill,
and it keeps the answer about the server rather than about him. It obeys
the same two fences a conversation does, the rate limit and the month's
budget, and when either says no you still get the free list.

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
| What he knows | his notes on every person, and the house shelf |
| What he has said | the ledgers that stop him repeating himself |
| The roster | last-seen times, and who made which colour role |
| The case book | warnings, cases, saved answers |
| His own posts | the message ids of what he keeps pinned |
| The install | bindings, feature switches, voting numbers, the house line |

Two things it never does. **It never touches an AI key** — that is the one
thing in the store that costs money to replace and cannot be read back off
Discord. And **it never deletes anything in Discord**: no channel, no role,
no message. Forgetting the colour-role registry does not delete the roles,
it means he stops knowing whose they are, and the reply says so.

`install.py` asks the same question when you point it at a server the disk
does not already belong to, and only then — a first install has nothing to
clear and is never asked.

## Running the server by asking

Everything a moderation bot does, minus the dashboard. Anyone in the
cooperative just says it, in a channel or a DM, and he acts on it — no
form to fill in, no separate permission to hold, no hunting for whoever
has one. Who may ask is checked in Python, not in his manners: nobody
outside the roll gets any of it, whatever they claim about who they are.

**The hands.** Warn, time out and lift one, kick, ban, unban, rename;
sweep a channel or one person's messages out of it; slow a channel down,
lock it, unlock it; put any role on anybody; post an announcement in his
voice. Every action is a numbered case in his book, naming who asked, and
a line in the log room.

**The signature.** The half of that which cannot be undone by saying
sorry — warn, timeout, kick, ban, sweep, and slowing, locking or
unlocking a room — waits for an administrator. Eugene takes the request
at face value, writes it up on a card in the log room with two buttons on
it, and tells you it is filed. It happens when somebody presses Approve,
and the card becomes the receipt. Nothing happens if nobody does: a
request lapses after an hour and leaves no case behind, because a list of
things that happened should not contain things that did not.

> *hadi: ban that spammer in #general*
> *Eugene: Written up and waiting on an admin — nothing done yet.*

He does not hold the conversation open while an administrator is found,
and he never reports a filed request as a done one. The person who signs
may be the person who asked, if they are an administrator; in a house
with one administrator any other rule is a house that cannot moderate
itself, and the log line says when the two hands were the same one.

The rest still happens on your word alone — lifting a timeout, unbanning,
renaming, roles, announcements, settings, forgiveness — because a gate on
everything is a gate nobody reads. So do the filters: automod acts on the
message in front of it, in the second after it was posted, and a spam
wave that waits for a signature is a spam wave that worked. If your house
wants none of this, `mod.require_signoff` turns it off and he goes back
to acting on the cooperative's word; `mod.signoff_minutes` is how long a
request stands.

**The machinery, set by talking.** No config file and no panel — say what
you want:

> *stop deleting links, we post GitHub all day*
> *greet people in #hellos and give them Newcomer*
> *three warnings should be an hour, not a day*
> *log deletes and edits into #mod-log*

Behind those sentences are welcomes and goodbyes, an arrival role, the
filters (banned words, invites, links with an allowlist, mention
pile-ups, shouting, flooding — each with its own cost), warning
escalation and expiry, a log room, and a shelf of stock answers. He
resolves channels
and roles by name, holds every number inside sane bounds, and says in one
line when a change makes the place less safe — after making it, not
instead.

Whole features go the same way — *turn the filters on*, *stop announcing
departures* — and that is the same switch the `/setup` panel shows, not a
second one. If you tune a feature that is switched off he says so rather
than storing a number nothing will read.

**What he refuses**, in code, however the request is dressed up:

- removing someone who is *in* the cooperative — that is a fundamental
  vote, and he files it for you rather than doing it;
- handing out the roles that decide who votes;
- anyone above him in the role list, or the server owner — Discord
  refuses, and he says the fix is moving his role up;
- anything that would reveal a ballot.

The first of those is a setting, because it is the house's rule and not
his: `mod.protect_cooperative`. Turn it off and one person can have
another removed with a sentence. He will tell you that, once, and then
do as he is told.

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

Setting one wakes Conversation, Memory of people and the Heartbeat
together, and the reply says which. **Details** says what is configured.
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
| `public_quorum_share` | the share of the server that must vote for an open poll to count | 0.2 |
| `kick_min_yes` | the fewest yes votes a removal can ever pass on | 3 |
| `away_days` | a quiet spell this long takes you out of the count | 14 |
| `role_create_max` | colour roles one person may make | 1 |
| `role_wear_max` | colour roles one person may wear at once | 5 |

Each is held inside a range where the rest of the machinery still means
what it says, so a share cannot be set above everybody and a window cannot
be set to nothing. Nothing is cached: a number changed at noon is the one
he quotes at one minute past, including on votes already open. Votes
already on the floor keep the window they were filed with.

## Two kinds of vote

**The cooperative's own business** — `/propose`, `/invite`, `/remove` — is
counted against the roster, so not voting is a no, and it passes the moment
the yes votes reach what is needed.

**A poll open to the whole server** — `/poll` — is counted against the
whole server. The roster rule would make every one of those fail on the day
it was filed, since most of a server has never asked to be counted in
anything, so a poll is carried by a majority of whoever votes, once
`public_quorum_share` of the room has voted at all. Under quorum it fails; a
tie fails; abstaining counts toward the quorum and toward neither side.

Both end early the moment they are decisive — a threshold reached for the
first, a lead bigger than the number of people left to vote for the second.
A poll is advisory: it never becomes a numbered decision, and acting on one
is a separate proposal.

**Nobody is DMed except about a priority vote**, which means one at the
fundamental tier (a removal, a rule change) or one whose author filed it as
priority with `/propose priority: True`. Ordinary proposals aren't chased,
and polls are never chased — a bot that DMs about everything teaches people
to ignore the one that mattered.

Each annex has a default model, chosen to be cheap rather than clever —
the clerk answers in a sentence or two, and a frontier model is several
times the price for a job that size. The modal's second field overrules
it for that server: put `claude-opus-5` or `gemini-3.1-pro` there and you
get a better writer at a higher bill. The spend counter prices the
defaults, so a server that changes model and cares about the figure
should set `price_in_per_m` and `price_out_per_m` in its settings file to
match. Claude answers with thinking off, which is what keeps a
one-sentence reply to one sentence.

By default he answers wherever he is mentioned. To keep him to one room,
point the `chat` job at a channel under **Rooms**: after that he talks
there, in its threads, and in direct messages, and nowhere else. Anyone
who mentions him elsewhere gets one short pointer to the right room, at
most once an hour, and it costs nothing to say. Leave `chat` unbound and
nothing changes.

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

## The heartbeat

The fourth proactive thing, and the only one that costs anything. Every
twenty minutes he wakes and works out — in plain Python, for nothing —
whether anything has actually happened: eight messages from two or more
people since he last looked, a vote closing within six hours with people
yet to vote, a passed decision nobody has carried out, a floor that has
been silent for ten days in a server that is talking. Almost every wake-up
ends there and costs zero.

When something has happened, he gets **one** thought: he reads the new
conversation and where governance stands, notes anything durable about the
people in it, and then decides whether the room is better off hearing from
him. He is told to answer "nothing" and usually does. What he can do
instead is say one short useful thing, or write out a proposal and offer
it.

**He is never the author.** A draft is posted with a button on it; whoever
presses it gets the proposal in a modal, every word editable, and it is
filed in *their* name. If nobody presses, nothing was proposed — which is
the right outcome for an idea only the bot had. He is also barred from
drafting anything about himself: his hosting, his budget, his model, what
he costs. If somebody wants that proposed, they can ask him and he files it
for them, which is a different thing.

Four fences, in this order, before a single token is spent:

| Fence | What it does |
|---|---|
| the gate | nothing has changed → back to sleep, cost nothing |
| `MAX_PER_DAY` | 12 thoughts a day, whatever the gate believes |
| budget floor | below 25% of the month left, the heartbeat stops entirely — being answered when you speak to him outranks being told something you did not ask |
| the topic ledger | anything he has raised is left alone for a fortnight |

## What he learns about people

He builds short notes on the people here from ordinary conversation, not
only from messages aimed at him — twelve notes each at most, the oldest
falling off, so it stays a sketch rather than a file. It is what makes him
know that one person argues for sport and another is never up before noon.

Three limits, and they are the point rather than the small print:

- **It is yours.** `/whatdoyouknow` shows you every note he holds on you,
  and `forget: True` deletes the lot. Ask him in a channel and he does the
  same thing without argument.
- **Deleting means deleted.** A strike also stops him learning about you,
  so it is not quietly rebuilt on the next pulse. Run the command again
  when you want him to start over.
- **Nobody reads anybody else's.** There is no parameter for whose profile
  to fetch, so there is nothing to talk him into. He is also told never to
  read notes aloud, never to tell one person what he knows about another,
  and never to use any of it to guess how somebody voted.

He only ever learns where he is allowed to speak. A server that binds a
`chat` room has kept him out of every other room entirely — listening
included.

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

1. **Variables**: `DISCORD_TOKEN`, `GUILD_ID`, `CLERK_DATA_DIR=/data`.
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
