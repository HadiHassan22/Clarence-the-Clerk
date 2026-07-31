"""Clarence the Clerk: first run, from a terminal.

Asks the four questions, writes .env, and puts any AI keys straight into
the per-server store where the clerk keeps them. Nothing here is required
(everything it does can also be done by hand, or from inside Discord
with `/setup`), but it is the short way in.

    python3 install.py

Secrets are read without echo and written 0600. The only things that
reach .env are the bot token and the server id; AI keys go to the
server's own settings file, the same place `/setup brain` puts them, so
there is one answer to "where is that key" rather than two.
"""

import base64
import getpass
import os
import subprocess
import sys
from pathlib import Path

import settings

HERE = Path(__file__).parent
ENV = HERE / ".env"

PERMISSIONS = 8  # Administrator: he makes roles, rooms and permissions


def ask(prompt, default="", secret=False, required=True):
    while True:
        shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
        value = (getpass.getpass(shown) if secret else input(shown)).strip()
        value = value or default
        if value or not required:
            return value
        print("  ...that one is needed.")


def yes(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{hint}]: ").strip().lower()
    return default if not answer else answer.startswith("y")


def token_fault(token):
    """Why this cannot be a bot token, or None if it might be.

    A token is typed into a prompt that shows nothing back, so a paste
    that lands twice looks exactly like a paste that lands once. Discord
    answers a doubled one with a bare 401 an hour later. Counting the
    three parts here costs nothing and names the real problem.
    """
    parts = token.split(".")
    if len(parts) == 3:
        return None
    if len(parts) == 5:
        return "that looks like two tokens run together, as a double paste does"
    return f"a bot token has three parts separated by dots; this has {len(parts)}"


def application_id(token):
    """A bot token carries its application id in the first segment, so
    the invite link can be built without asking for it twice."""
    head = token.split(".")[0]
    try:
        decoded = base64.b64decode(head + "=" * (-len(head) % 4)).decode()
        return decoded if decoded.isdigit() else None
    except (ValueError, UnicodeDecodeError):
        return None


def invite_url(token):
    app_id = application_id(token)
    if not app_id:
        return None
    return (
        f"https://discord.com/oauth2/authorize?client_id={app_id}"
        f"&permissions={PERMISSIONS}&scope=bot%20applications.commands"
    )


def write_env(token, guild_id):
    if ENV.exists() and not yes(f"{ENV.name} exists. Overwrite it?", default=False):
        print("Left it alone. The rest of this still applies.")
        return
    ENV.write_text(
        "# Written by install.py. The bot token and the server it keeps.\n"
        "# AI keys are not here: each server holds its own, set with\n"
        "# /setup brain in Discord or by this script.\n"
        f"DISCORD_TOKEN={token}\n"
        f"GUILD_ID={guild_id}\n"
    )
    os.chmod(ENV, 0o600)
    print(f"  wrote {ENV.name} (0600)")


def main():
    print(__doc__.split("\n\n")[0])
    print()

    while True:
        token = ask("Bot token (Developer Portal -> Bot -> Reset Token)", secret=True)
        fault = token_fault(token)
        if fault is None:
            break
        print(f"  ...{fault}. Copy it again and paste once.")

    link = invite_url(token)
    if link:
        print("\nInvite the bot to your server with this, if you have not:")
        print(f"  {link}")
        print("It asks for Administrator, which the clerk needs to shape "
              "channels and hand out the Key role.\n")
    else:
        print("\n(That token does not look like a bot token, so carrying on "
              "anyway, but check it if the clerk will not start.)\n")

    guild_id = ask("Server id (Developer Mode on, right-click the server -> Copy Server ID)")
    if not guild_id.isdigit():
        print("A server id is all digits. Start again when you have it.")
        return 1

    write_env(token, guild_id)

    print(
        "\nThe clerk can talk if you give him a key. Gemini is Google AI "
        "Studio,\nGrok is console.x.ai, Claude is console.anthropic.com. "
        "Any of them, all\nof them, or none: leave blank to skip, and he "
        "stays a clerk who does not\nspeak. You can add one later with "
        "/setup brain.\n"
    )
    data = Path(os.environ.get("CLERK_DATA_DIR", HERE))
    data.mkdir(parents=True, exist_ok=True)
    settings.configure(data)
    given = []
    for name, label in (("gemini", "Gemini"), ("grok", "Grok"),
                        ("claude", "Claude")):
        key = ask(f"{label} API key (blank to skip)", secret=True, required=False)
        if key:
            settings.set_brain_key(int(guild_id), name, key, by="install.py")
            given.append((name, label))
            print(f"  stored the {label} key for server {guild_id} "
                  f"({settings.fingerprint(key)})")
    if len(given) > 1:
        settings.set_provider(int(guild_id), given[0][0])
        print(f"  {given[0][1]} will do the talking; /setup use switches.")

    print("\nDone. What is left:")
    print("  1. Turn on the Server Members and Message Content intents in the")
    print("     Developer Portal (Bot -> Privileged Gateway Intents).")
    print("  2. Drag the clerk's role to the top of the role list.")
    print("  3. Run the clerk:")
    print("       python3 clerk.py")
    print("  4. In Discord, run /setup start. It makes the roles and the")
    print("     governance rooms, and puts you in the cooperative -- until")
    print("     somebody is in it, he will not talk to anyone.")

    # Governance rooms only, and said out loud. The first version of this
    # built the whole starting layout -- memes, pets, gaming, voice -- into
    # servers that had their own rooms already and had not asked for any of
    # it. `/setup start` does the same job from inside Discord, so this is
    # now the shortcut and not the route.
    print(
        "\nOptionally, build the governance rooms from here instead: propose,\n"
        "votes, decisions, polls, bot-health and the archive. Nothing else in\n"
        "your server is touched. (build_server.py --full-layout builds the\n"
        "whole hangout too, which only suits an empty server.)"
    )
    if yes("Build the governance rooms now?", default=False):
        return subprocess.call([sys.executable, str(HERE / "build_server.py")])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Nothing further was written.")
        sys.exit(130)
