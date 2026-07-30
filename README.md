# Clarence the Clerk

The executive of **The Hangout**: a Discord server for a small group of
friends, run as an experiment in direct self-governance. No moderators,
no admins; a charter at the door, a parliament of everyone, and this bot
as the only officeholder.

How it works, in one breath: the server is invisible until you sign the
charter, signing hands you the Key; any member files a bill (title, what,
why), Clarence opens a debate chamber (text + voice) and an anonymous
ballot; when the floor closes, passed bills become numbered Acts in the
gazette, the notes survive as the record, the chamber is archived, and
Clarence executes what passed. Members also get self-service aesthetic
roles (create 5, wear 5) in #roles.

The founding documents: [constitution.md](constitution.md) (the charter),
[standing-orders.md](standing-orders.md) (rules of procedure, filed as
Bill No. 1). Design history and scopes: [ROADMAP.md](ROADMAP.md),
[voting-system-scope.md](voting-system-scope.md),
[roles-scope.md](roles-scope.md). Brand kit: [branding/](branding/).

## The two programs

- **`clerk.py`**: the resident daemon. This is what gets hosted 24/7.
- **`build_server.py`**: the one-shot structure builder. Run locally by a
  human whenever [server_config.yaml](server_config.yaml) changes: it
  sweeps stray channels into the archive, builds/updates all channels,
  permissions and ordering, and posts the charter. Idempotent; safe to
  re-run. Deliberately never hosted.

Both read `server_config.yaml`, so config changes should be committed and
deployed, then built locally from the same commit.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in DISCORD_TOKEN and GUILD_ID
.venv/bin/python build_server.py   # build the structure
.venv/bin/python clerk.py          # run the clerk
```

The bot needs Administrator and the Server Members intent, with its role
at the top of the role list.

## Hosting (Render)

Deploy as a **Background Worker** (no HTTP port), start command
`python clerk.py`. Environment: `DISCORD_TOKEN`, `GUILD_ID`,
`PYTHON_VERSION` (3.13), and `CLERK_DATA_DIR=/data` pointing at a
persistent disk so state (signatures, bills, acts, roles) survives
deploys. The builder stays on a human's machine; give the worker the
production `GUILD_ID` and run the builder locally against the same ID.

State files are runtime data and are gitignored on purpose.
