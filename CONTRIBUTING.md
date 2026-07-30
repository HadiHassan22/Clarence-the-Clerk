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
cp .env.example .env      # DISCORD_TOKEN, GUILD_ID; GEMINI_API_KEY if you
                          # want the brain awake
.venv/bin/python build_server.py   # builds the structure, one shot
.venv/bin/python clerk.py          # runs the clerk
```

Set `floor_hours` in `server_config.yaml` to something tiny (0.05 is three
minutes) while testing so bills close while you are still watching. Never
commit that change.

## House rules

- **Never commit secrets or state.** `.env` and the JSON state files
  (`signatures.json`, `bills.json`, `acts.json`, `roles.json`,
  `clerk_state.json`, `executor_log.json`, `brain_state.json`) are
  gitignored on purpose. They are the server's memory and live on the
  host's disk.
- **`build_server.py` is never hosted.** It is a one-shot tool a human runs
  deliberately, because it sweeps and re-permissions channels.
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
  health endpoint). This is what gets hosted.
- `brain.py`: the conversational layer (Gemini), gated to holders of the
  `bot-whisperers` role, rate limited and budget capped.
- `toolbox.py`: the harness. The only door between the model and reality.
- `build_server.py` + `server_config.yaml`: the server's structure.
- `constitution.md`, `standing-orders.md`: the founding documents; the
  clerk reads them into his own system prompt. Treat their wording as
  precious, it was argued over at length.
- `ROADMAP.md`: where this is going.
