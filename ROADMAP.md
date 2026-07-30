# Roadmap

Everything here is design until the bot executes it. The principle: nothing
is built by hand. When the bot first runs, it creates the whole server in
one shot from these scopes, posts the charter, and becomes the executive.

## Phase 0: seed (current)

Handled by Salah by hand: temp channels, provisional polls, cleanup.
Everything pre-Act-1 is provisional and will be overwritten by the bot.
All third-party bots are already removed.

## Phase 1: channels

- The minimal channel set lives in server_config.yaml. Nothing speculative:
  a channel earns its place or waits to be voted in later.
- Layout: #charter alone at the top (ceremonial, read-only), hangout
  category (the actual point of the server), civic category at the bottom.
- Voice design deliberately deferred; one General VC survives for now.

## Phase 2: branding

- Server name: seeded by the poll, finalized before launch.
- Then, derived from the name: server description, icon, banner.
- The bot's own name and avatar are part of this pass; the bot publishes
  every official document, so it reads as the institution.

## Phase 3: the bot, one shot

- Rewrite the bot around the final design. On first run it:
  1. **Sweeps the polluted pre-launch channels**: every existing text
     channel is renamed to archived_<name>, locked (send denied), and
     moved into the 🗄️ archive category. Nothing is deleted; history
     survives. Existing voice channels are simply deleted (voice has no
     history to preserve). Existing categories are removed once emptied.
  2. Builds the full structure from server_config.yaml.
  3. Posts the charter to #charter as a sequence of embeds (one per
     section, shared accent color).
  4. Opens the governance machinery (#submit-a-bill button, #the-floor,
     #gazette).
- The current bot.py and build_server.py are prototypes from an earlier
  design (join gating, ban votes, Admin/Member/Pending roles). Superseded.
  Useful scraps: vote persistence, tally logic, idempotent channel builder.
- **Deployment** (needed from this phase on, 24/7):
  - Options: cheap VPS (Hetzner ~4 EUR/mo, DigitalOcean ~6 USD/mo),
    a Raspberry Pi or spare always-on machine at home, Oracle free tier.
  - Shape: Python 3.11+, systemd service, token in .env, votes and
    backups on disk. The off-server backup from the charter lives here.
  - Decide when we get here; a laptop is fine for testing, not for launch.
- **Music: self-hosted, ours.** (Decided July 2026: build it, don't adopt
  a third-party bot.) Music playback for the three music VCs, running on
  the same host: either inside Clarence or as a small companion process
  sharing the codebase. Decide the process split at build time; playback
  crashes must never take down governance.

## Phase 4: the voting system

- Full scope in voting-system-scope.md (bill templates with what/why,
  named + anonymous note slots, untracked button ballots).
- The bot reads thresholds and windows from config; the parliament
  legislates the values.

## Phase 5: member roles

- Full scope in roles-scope.md (member-created cosmetic roles, name + hex
  color, the quiet enabler of political parties).

## Founding sequence on launch day

1. Bot joins, builds everything, posts the charter.
2. Act 1: ratify the charter (counted under the charter's provisional
   rule: more yes than no among votes cast).
3. Act 2: first ownership election, ownership actually transferred.
4. Bill No. 1, on the floor from minute one: the Standing Orders
   (standing-orders.md). Doubles as the demonstration of what a bill
   looks like. Becomes Act 3.
5. Parliament legislates freely from there (quorums, windows, roles,
   whatever it wants).
