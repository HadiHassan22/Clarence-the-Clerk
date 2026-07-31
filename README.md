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
- **`install.py`**: the first-run wizard. Asks four questions, writes
  `.env`, stores any AI keys, prints your invite link.
- **`build_server.py`**: the structure builder, from a terminal. By
  default it builds only the governance rooms from
  [server_config.yaml](server_config.yaml) and touches nothing else;
  `--full-layout` builds the whole starting server, which only suits an
  empty one. Idempotent.
- **`builder.py`**: the shaping itself, shared by `build_server.py` and
  the `/setup` commands so both do exactly the same thing.
- **`settings.py`**: per-server settings, keyed by guild id: the AI
  keys, the model, the budget.
- **`duties.py`**: the short list of things the clerk says without being
  asked, and the ledger that stops him saying any of them twice.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python install.py        # writes .env, prints your invite link
.venv/bin/python clerk.py          # run the clerk
```

Then, in your server: **`/setup start`**. It makes the `Cooperative` and
`Member` roles, binds them, creates the governance channels that do not
exist yet, and — the part that matters — **puts you in the cooperative**.
Until somebody is in it, Eugene refuses everyone, including whoever
installed him.

After that, `/setup grant` hands the cooperative to anyone else who should
have a vote. Once there are a few of you, the ordinary way in is
`/invite`, which is a vote. `/setup brain` gives him something to think
with; `/setup status` says what is still unconfigured.

**Nothing here names a server.** Eugene reads the name off Discord, so
renaming the server is the whole of it — proposal text, his own
description of himself and the health card all follow. What he cannot
read off Discord is what the place is *for*, so `/setup house` takes one
line of it:

```
/setup house description: a book club that argues about endings
```

Leave it unset and he describes the server neutrally rather than guessing.
Run it with no text to reset.

Everything `/setup` creates is additive. It never renames, moves,
re-topics, re-permissions or deletes a channel you already had, and it
only ever makes governance rooms — no hangout, no memes channel, nothing
you did not ask for. The full starting layout exists for an empty server
and has to be asked for by name (`build_server.py --full-layout`).

The bot needs Administrator with its role at the top of the role list,
and the **Server Members** and **Message Content** privileged intents
enabled in the Developer Portal. If you cannot enable Message Content,
set `CLERK_MESSAGE_CONTENT=0`; everything works except talking.

## What members can type

The same governance the buttons give you, without hunting for the button.
Every one of these does its work directly — no model is consulted and
nothing is spent, where asking him for the same thing in a channel costs
a conversation:

- **`/propose`** — file a proposal: title, what, why.
- **`/invite`** — propose that someone be let in.
- **`/remove`** — propose that someone be removed. Says what the
  instrument costs before it asks who.
- **`/close <number>`** — call time on a vote that has had its run.
- **`/bills`** — what is open for a vote right now.
- **`/role`** — make or manage your colour.

They are the cooperative's, and refuse anyone else. The `/setup` group
below is the administrators'.

## Giving the clerk a brain

The clerk keeps records and holds the door with no AI at all. To make him
talk, someone with the keys to the server sets a key from inside Discord:

```
/setup brain annex:Gemini    # a key from Google AI Studio
/setup brain annex:Grok      # a key from console.x.ai
/setup brain annex:Claude    # a key from console.anthropic.com
```

Any one will do, or several. `/setup use` switches which one speaks, and
`/setup forget-brain` takes one away again. Keys are typed into a private
modal, checked against the API before they are saved, and kept in the
server's own settings file at `0600`; only the last four digits are ever
shown or logged. Each server pays for its own thinking and has its own
monthly budget, spend counter and rate limits.

`/setup status` says what is configured. The whole `/setup` group is
restricted to administrators.

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
point the `chat` job at a channel in `/setup rooms`: after that he talks
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
`CLERK_DATA_DIR`. After the first deploy, either re-run `/setup brain` in
Discord (simplest) or copy `guilds/` onto the disk.

State files are runtime data and are gitignored on purpose. So is
`guilds/`, which holds the keys. Never commit it.
