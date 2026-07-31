"""Eugene: shaping a server, from a terminal.

A thin wrapper around builder.py, which does the actual work and is
shared with `/setup` inside Discord. Use whichever is to hand: this one
wants the repo and a filled-in .env, that one wants nothing but the bot
in your server and the keys to the place.

By default it builds only the governance rooms: somewhere to propose,
somewhere to vote, somewhere to publish the result, and the archive. Your
hangout is yours. A bot that turns up and makes a memes channel nobody
asked for has overstepped, so the rest of the starting layout has to be
asked for by name.

Idempotent: safe to re-run. Channels matching the config are adopted and
updated.

Usage:
    .venv/bin/python build_server.py                 # governance rooms only
    .venv/bin/python build_server.py --full-layout   # the whole starting server
    .venv/bin/python build_server.py --full-layout --sweep
"""

import os
import sys
from pathlib import Path

import discord
import yaml
from dotenv import load_dotenv

import builder

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

CONFIG = yaml.safe_load((HERE / "server_config.yaml").read_text())

intents = discord.Intents.default()
client = discord.Client(intents=intents)


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
        lacking = builder.missing_permissions(guild)
        if lacking:
            raise RuntimeError(
                "Eugene is missing permissions here: "
                + ", ".join(lacking)
                + ". Grant them and re-run."
            )
        full = "--full-layout" in sys.argv
        # --sweep is the destructive one: it archives every text channel
        # not in the config and deletes every voice channel not in it.
        # Only ever right on a fresh server whose config you wrote.
        sweeping = "--sweep" in sys.argv
        if sweeping and not full:
            # Against the governance-only config, sweeping would archive
            # every room the filter dropped -- which is the whole server.
            raise RuntimeError(
                "--sweep only makes sense with --full-layout. On its own it "
                "would archive every channel outside the governance rooms, "
                "which is all of them."
            )
        config = CONFIG if full else builder.governance_only(CONFIG)
        if full:
            print("!! --full-layout given: building the whole starting server,")
            print("!! hangout and voice rooms included.")
        else:
            kept = ", ".join(c["name"] for c in config["categories"])
            print(f"Building the governance rooms only: {kept}.")
            print("Nothing else here is touched. --full-layout builds the rest.")
        if sweeping:
            print(
                "!! --sweep given: channels missing from server_config.yaml\n"
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
