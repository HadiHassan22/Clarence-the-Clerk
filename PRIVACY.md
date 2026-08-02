# What leaves your server

Eugene is a governance bot with an optional conversational layer. This page
says exactly what is sent where, written from the code, with the line that
does it. If you change one of those lines, change this page in the same
commit: a privacy policy that has drifted is worse than none.

## With no AI key, nothing leaves

This is the important sentence and it is worth stating first. Proposals,
ballots, the record, the roster, thresholds, reminders and the standing list
of unfinished decisions are all plain Python over files on the host's disk.
No model is consulted for any of it, and a server that never sets a key
never sends a byte to anybody.

Every governance feature declares `"brain": False` in `modules.py`. The
conversational layer is off until somebody sets a key from `/setup`.

## He does not listen in rooms he cannot answer in

`brain.py` gates remembering on the same condition as speaking
(`handle_message`, `brain.py:981`): the call that stores a message is inside
`if message.content and speaks_here`. So a server that pens him into one
channel has penned him out of every other one, for reading as well as for
talking. `may_speak_in` (`brain.py:948`) is where "may speak here" is
decided, and `_is_his` (`brain.py:913`) is the fallback when no chat room is
bound: his own rooms, and nothing else.

`/access` prints this for your server, computed live: what he can see, what
he listens in, what he answers in, and what is invisible to him.

**The one exception, stated plainly.** `clerk.py:on_message` calls
`roster.touch(guild.id, author.id)` for every message in every channel he can
see, including ones he will never read or answer in. That writes a user ID
and a timestamp to a file on the host's disk, and nothing else — no content,
no channel, no message ID. It is what the away rule is counted from. It never
leaves the host.

## When somebody talks to him, this is the request

Sent only when a member of the cooperative addresses him in a room he may
answer in.

| What | How much | Where in the code |
|---|---|---|
| Recent messages from **that room**, with display names | up to 40, 600 chars each | `_remember` / `_transcript`, `brain.py:591` |
| Tool calls he made in that room and what came back | up to 14, 700 chars each | `_note_deed` / `_deed_log`, `brain.py:646` |
| The message being answered, and who sent it | one | `_run_turn`, `brain.py:915` |
| The message it is a reply to, and who sent it | one, 600 chars | `_quoted`, `brain.py:862` |
| Roster size, how many are away, what a vote needs today | a line | `_roster_now`, `brain.py:340` |
| Which features are switched on and off | a line | `_switches`, `brain.py:407` |
| Server name, and the one-line description if `/setup` set one | a line | `_system_prompt` |
| The standing orders brief, and his instructions | fixed text | `standing-orders.md`, `brain.py` |
| Tool definitions for the features you have on | fixed text | `toolbox.declarations` |

Roughly 4,000 tokens of fixed text plus the room's recent conversation.

Replying to a message is what sends it. Discord shows the person who wrote it
what their reply is attached to, and it is half of what the reply means, so it
goes with the reply. That is the one thing here that can be older than the
last forty lines, and it is fetched from the room the reply was sent in and
nowhere else.

**What is not sent:** individual ballots, ever, to anybody — they are
stripped before anything reaches the model and destroyed when a vote closes.
Nothing about members of the server who are not in the room. No durable
profile of anybody: Eugene keeps no notes on people at all, by design, so
there is nothing about a person to send.

Nothing is sent on a timer. He speaks when spoken to; there is no background
job that reads a room and decides to say something.

## Who receives it

Whichever provider the server's key belongs to, and only that one. Keys are
per server, stored `0600` under `guilds/<id>/settings.json`, never logged and
never shown — only a fingerprint (`settings.fingerprint`). One server's
messages never reach another server's provider, and one server's bill is
never another's.

| Provider | Their terms |
|---|---|
| Anthropic (Claude) | <https://www.anthropic.com/legal/commercial-terms> · <https://privacy.anthropic.com> |
| Google (Gemini) | <https://ai.google.dev/gemini-api/terms> |
| xAI (Grok) | <https://x.ai/legal/terms-of-service> |
| OpenAI | <https://openai.com/policies/> |

Retention and training policies are **theirs and they change**. Read the
current terms for whichever you set rather than trusting a summary here.
Free tiers in particular often differ from paid ones on whether input is used
for training.

## What is kept, and where

On the host's disk, under `guilds/<server id>/`, never in this repository:

| File | What |
|---|---|
| `bills.json` | proposals: title, what, why, author, status, tally |
| `acts.json` | decisions on the record |
| `roster.json` | user IDs and last-seen timestamps |
| `duties.json` | what he has already said, so he does not repeat it |
| `clerk_state.json` | message IDs of things he keeps pinned, the bill counter |
| `settings.json` | this server's settings **and its AI key** |

Individual ballots live on a proposal only while the vote is open and are
destroyed at close; the tally survives, the votes do not.

A veto is held the same way. While the window on a passed proposal is open,
the ids of whoever has vetoed sit on the proposal, for one purpose: stopping
one person casting two. They are destroyed when the window shuts, whether or
not it overturned anything. What survives is how many, and — where the house
has not switched the veto to anonymous, so the names were already on the
decision in the open — who.

`logs/` holds an audit line for every tool the model was allowed to run:
which tool, its arguments, and what came back. Operational, on the host, and
gitignored.

## What you can do

- **`/privacy`** — any member, not just admins. Prints this for your server,
  computed live: which room he listens in, how many messages he is holding,
  which provider is on duty, and what has been spent this month.
- **`/access`** — what he can see in your server, worked out from Discord's
  own permissions rather than from anything written down.
- **Purge** — clears the transcript and tool results he is holding in memory
  right now. They are in the process, never on disk, and a restart clears
  them anyway.
- **Remove the key** — `/setup`, and nothing leaves the server again.

## Consent

An administrator has to see a summary of this and accept it before a key can
be set. Who accepted and when is recorded in `guilds/<id>/settings.json`.

That is one admin accepting on behalf of a server's members, which is a real
limitation and worth saying out loud rather than dressing up: if that is not
good enough for your house, the honest answer is to leave the AI layer off.
Everything Eugene exists to do works without it.
