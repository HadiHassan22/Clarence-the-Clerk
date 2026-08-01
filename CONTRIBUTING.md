# Contributing

`main` is the published branch, and installs track it. Treat a merge as a
release.

## The workflow

You work from a **fork**. Nobody pushes to `main` directly.

```sh
# once
git clone https://github.com/<you>/Clarence-the-Clerk.git
cd Clarence-the-Clerk
git remote add upstream https://github.com/<owner>/Clarence-the-Clerk.git

# per change
git fetch upstream && git checkout -b my-change upstream/main
# ... work ...
git push origin my-change
# then open a pull request against upstream: main
```

A maintainer reviews and merges. Nothing else reaches `main`.

## Setting up to run it

You need your own bot and your own server; never test against the live one.

1. Create a Discord application in the developer portal, add a bot, copy
   its token, enable the **Server Members** and **Message Content**
   privileged intents.
2. Create an empty Discord server, invite your bot with Administrator, and
   drag its role to the top of the role list.
3. Copy the server ID (Developer Mode on, right-click the server icon).

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python install.py        # asks, writes .env, prints the invite link
.venv/bin/python clerk.py          # runs the clerk
```

Then `/setup` in your sandbox server: **Apply** builds the structure and
puts you in the cooperative, and **Brain** wakes him with your own key.
`build_server.py` does the same building from a terminal if you prefer,
off the same plan — `--list` prints what it would build without building
it. Nothing about the brain goes in `.env`: keys are per server and live
in `guilds/<id>/settings.json`, which is gitignored.

Switch off whatever you are not working on. **Features** on the panel is
the whole list, and a feature that is off costs nothing to have around:
no rooms built, no events handled, and its tools are not even declared to
the model.

Set the vote window to something tiny while testing so bills close while
you are still watching. **Numbers** on the panel, `floor_hours` → `0.05`,
is three minutes and touches no tracked file — that is the way to do it.
`floor_hours` in `server_config.yaml` still works as the default a fresh
server starts from, but it is a tracked file, so never commit that
change.

Run `python3 tests.py` before opening a pull request. The settings and
harness tests need nothing but the standard library; the rest skip loudly
if `requirements.txt` is not installed.

## House rules

- **Never commit secrets, state, logs or raw data.** `.env`, the JSON state
  files (`signatures.json`, `bills.json`, `acts.json`, `roles.json`,
  `clerk_state.json`, `brain_state.json`), the `logs/` directory (which is
  where `executor_log.json` lives) and the whole `guilds/` directory are
  gitignored on purpose. They are the server's memory and live on the
  host's disk. `guilds/` also holds every server's AI keys, so treat it
  the way you would treat `.env`.
- **Raw material goes in `raw/`,** which is gitignored for a reason worth
  spelling out. A channel export is every member's messages, display names
  and user ids; it is fine to keep one locally to work from, and it is not
  fine to publish one to a repo people fork. If you need it in the repo,
  you need a summary of it instead.
- **A server's AI key is shown to nobody.** It is entered in a modal,
  answered ephemerally, stored `0600`, and only ever displayed or logged
  through `settings.fingerprint()`. Never put one in a log line, an error
  message, a health post or a channel.
- **Building is destructive and must stay deliberate.** `builder.py`
  sweeps and re-permissions channels and deletes stray voice channels,
  and only ever when `--sweep` is asked for by name at a terminal. From
  inside Discord, **Apply** is strictly additive: it creates and adopts,
  and never renames, moves, re-topics, re-permissions or deletes anything
  that already existed. Keep that line where it is, and keep the preview
  honest — somebody should be able to read what is about to happen before
  it happens.
- **A feature is a module.** Anything new that a server might reasonably
  not want goes in `modules.py` with its rooms, roles, settings and tools
  declared, and every entry point checks it. A switch that half the code
  respects is worse than no switch. The default is whatever the clerk
  already did, so an upgrade in place is never a change of behaviour.
- **The clerk's brain is harnessed.** The model may only call tools in the
  registry in `toolbox.py`. Do not give it shell access, `eval`, raw
  discord API calls, or any tool that deletes something. Adding a tool
  means adding a hand-written handler with a typed schema, and major
  (structural) tools must be gated behind a passed Act.
- **Individual votes are sealed**, including from the bot's own brain.
  Anything that reads `bills.json` for the model must strip `ballots`, and
  people-bills (invitations, removals) must never expose a tally.
- **No em dashes** in any user-facing text. Colons, commas, or separate
  sentences instead.
- Match the surrounding style: no docstring novels, no comments explaining
  what the next line does, dry wit in the bot's own voice.

## The shape of the thing

- `clerk.py`: the resident daemon (door, bills, ballots, chambers, roles,
  health endpoint, the `/setup` panel). This is what gets hosted.
- `modules.py`: what he does here, in twelve switchable parts, and the
  single description of the governance layout. Discord-free, so all of it
  is testable on a laptop.
- `slate.py`: whose history is on this disk, and clearing it by scope.
  Anything new that gets written to the data root belongs in one of its
  scopes, or it will follow the daemon to the next server it serves.
- `brain.py`: the conversational layer, gated to holders of the
  `bot-whisperers` role, rate limited and budget capped. Knows nothing
  about any API's wire format.
- `providers.py`: the annexes. Gemini, Grok and Claude behind one
  interface, so adding a fourth is one class and no edits elsewhere.
- `settings.py`: per-server settings and keys, keyed by guild id. Pure
  standard library; keep it that way so it stays testable anywhere.
- `toolbox.py`: the harness. The only door between the model and reality.
- `builder.py`: shaping a server. Shared by `build_server.py` and
  `/setup` so a terminal and Discord cannot drift apart.
- `install.py`: the first-run wizard.
- `build_server.py`: the terminal route into a server, off the plan in
  `modules.py`. `server_config.yaml` is now only the hangout, for laying
  out an empty server with `--full-layout`.
- `constitution.md`, `standing-orders.md`: the founding documents; the
  clerk reads them into his own system prompt. Treat their wording as
  precious, it was argued over at length.
- `ROADMAP.md`: where this is going.
