"""Eugene: shaping a server, from a terminal.

A thin wrapper around builder.py, which does the actual work and is
shared with `/setup` inside Discord. Use whichever is to hand: this one
wants the repo and a filled-in .env, that one wants nothing but the bot
in your server and the keys to the place.

By default it builds exactly what `/setup` would build -- the rooms the
features you have switched on ask for, and the archive. Both routes read
the same plan out of `modules.py`, so it does not matter which one you
use, and using both does not give you two of everything. Switch a feature
off and its rooms stop being built here too.

Your hangout is yours. A bot that turns up and makes a memes channel
nobody asked for has overstepped, so the rest of the starting layout --
the hangout, the voice rooms, the front door, all of it still in
server_config.yaml -- has to be asked for by name.

Idempotent: safe to re-run. Channels matching the plan are adopted and
updated.

Usage:
    .venv/bin/python build_server.py                 # what the features want
    .venv/bin/python build_server.py --list          # print the plan, build nothing
    .venv/bin/python build_server.py --full-layout   # that, plus the hangout
    .venv/bin/python build_server.py --full-layout --sweep
"""

import os
import sys
from pathlib import Path

import discord
import yaml
from dotenv import load_dotenv

import builder
import modules
import settings

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

# The same store the daemon reads, resolved the same way, so a room built
# here is a room Eugene already knows the job of, and the switches this
# reads are the ones somebody set in `/setup`. Without this the builder
# made the channels and told nobody, and setting up from inside Discord
# afterwards had no way to tell "already built" from "not built yet".
settings.configure(Path(os.environ.get("CLERK_DATA_DIR", HERE)))

CONFIG = yaml.safe_load((HERE / "server_config.yaml").read_text())

intents = discord.Intents.default()
client = discord.Client(intents=intents)


def describe(config):
    for cat in config["categories"]:
        rooms = [c["name"] for c in cat.get("channels") or []]
        print(f"  {cat['name'] or '(no category)'}: "
              + (", ".join(rooms) if rooms else "(empty)"))


def plan_for(guild_id, full):
    """What will be built. The module half always; the hangout only when
    it has been asked for by name."""
    plan = builder.from_modules(guild_id)
    if full:
        plan = builder.merge(plan, builder.hangout_only(CONFIG))
    return plan


def print_plan(guild_id, full):
    on = [modules.name(k) for k in modules.keys()
          if modules.enabled(guild_id, k)]
    off = [modules.name(k) for k in modules.keys()
           if not modules.enabled(guild_id, k)]
    print(f"Features on:  {', '.join(on) or 'none'}")
    print(f"Features off: {', '.join(off) or 'none'}")
    print("Rooms this adds up to:")
    describe(plan_for(guild_id, full))


@client.event
async def on_ready():
    exit_code = 0
    try:
        guild = client.get_guild(GUILD_ID)
        if guild is None:
            raise RuntimeError(
                f"Eugene is not in a server with id {GUILD_ID}. Check "
                f"GUILD_ID in .env, and that the bot has been invited."
            )
        print(f"Clerk on duty in: {guild.name}")
        full = "--full-layout" in sys.argv
        if "--list" in sys.argv:
            print_plan(guild.id, full)
            print("\n(--list given: nothing was built.)")
            return
        lacking = builder.missing_permissions(guild)
        if lacking:
            raise RuntimeError(
                "Eugene is missing permissions here: "
                + ", ".join(lacking)
                + ". Grant them and re-run."
            )
        # --sweep is the destructive one: it archives every text channel
        # not in the plan and deletes every voice channel not in it. Only
        # ever right on a fresh server whose layout you chose yourself.
        sweeping = "--sweep" in sys.argv
        if sweeping and not full:
            # Against the module-derived plan, sweeping would archive every
            # room outside the governance ones -- which is the whole server.
            raise RuntimeError(
                "--sweep only makes sense with --full-layout. On its own it "
                "would archive every channel outside the rooms your features "
                "asked for, which is all of them."
            )
        config = plan_for(guild.id, full)
        print_plan(guild.id, full)
        if full:
            print("!! --full-layout given: the hangout and voice rooms from")
            print("!! server_config.yaml are being built too.")
        else:
            print("Nothing else here is touched. --full-layout builds the "
                  "hangout as well.")
        if sweeping:
            print(
                "!! --sweep given: channels missing from the plan above\n"
                "!! will be ARCHIVED, and voice channels DELETED."
            )
        await builder.install(guild, config, say=print, sweep_existing=sweeping)
    except Exception as e:
        print(f"FAILED: {e!r}")
        exit_code = 1
    finally:
        await client.close()
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    client.run(TOKEN)
