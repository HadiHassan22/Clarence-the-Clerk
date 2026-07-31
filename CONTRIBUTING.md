# Contributing

This repo runs a live Discord server for a group of friends. `main` is the
published branch: whatever lands there deploys to the real server within
about two minutes. Treat a merge as a release.

## The workflow

You work from a **fork**. You never push to this repository directly.

```sh
# once
git clone https://github.com/<you>/Clarence-the-Clerk.git
cd Clarence-the-Clerk
git remote add upstream https://github.com/salahalshayah/Clarence-the-Clerk.git

# per change
git fetch upstream && git checkout -b my-change upstream/main
# ... work ...
git push origin my-change
# then open a pull request against salahalshayah/Clarence-the-Clerk: main
```

Salah reviews and merges. Nothing else reaches `main`.

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

Then `/setup server` in your sandbox server builds the structure, and
`/setup brain annex:Gemini` (or `annex:Grok`, or `annex:Claude`) wakes
the brain with your own key. `build_server.py` does the same building
from a terminal if you prefer. Nothing about the brain goes in `.env`:
keys are per server and live in `guilds/<id>/settings.json`, which is
gitignored.

Set `floor_hours` in `server_config.yaml` to something tiny (0.05 is three
minutes) while testing so bills close while you are still watching. Never
commit that change.

Run `python3 tests.py` before opening a pull request. The settings and
harness tests need nothing but the standard library; the rest skip loudly
if `requirements.txt` is not installed.

## House rules

- **Never commit secrets or state.** `.env`, the JSON state files
  (`signatures.json`, `bills.json`, `acts.json`, `roles.json`,
  `clerk_state.json`, `executor_log.json`, `brain_state.json`) and the
  whole `guilds/` directory are gitignored on purpose. They are the
  server's memory and live on the host's disk. `guilds/` also holds every
  server's AI keys, so treat it the way you would treat `.env`.
- **A server's AI key is shown to nobody.** It is entered in a modal,
  answered ephemerally, stored `0600`, and only ever displayed or logged
  through `settings.fingerprint()`. Never put one in a log line, an error
  message, a health post or a channel.
- **Building is destructive and must stay deliberate.** `builder.py`
  sweeps and re-permissions channels and deletes stray voice channels.
  It runs from `build_server.py` at a terminal, or from `/setup server`,
  which is administrator-only and asks for confirmation first, spelling
  out what it will delete. Keep both of those guards.
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
  health endpoint, the `/setup` group). This is what gets hosted.
- `brain.py`: the conversational layer, gated to holders of the
  `bot-whisperers` role, rate limited and budget capped. Knows nothing
  about any API's wire format.
- `providers.py`: the annexes. Gemini, Grok and Claude behind one
  interface, so adding a fourth is one class and no edits elsewhere.
- `settings.py`: per-server settings and keys, keyed by guild id. Pure
  standard library; keep it that way so it stays testable anywhere.
- `toolbox.py`: the harness. The only door between the model and reality.
- `builder.py`: shaping a server. Shared by `build_server.py` and
  `/setup server` so a terminal and Discord cannot drift apart.
- `install.py`: the first-run wizard.
- `build_server.py` + `server_config.yaml`: the server's structure.
- `constitution.md`, `standing-orders.md`: the founding documents; the
  clerk reads them into his own system prompt. Treat their wording as
  precious, it was argued over at length.
- `ROADMAP.md`: where this is going.
