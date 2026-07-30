# Voting System Scope

Status: scoped, not started. Parked until the seed phase of the server is done.
This is the system Salah has in mind, captured for later implementation.

## Terminology

A proposed law is a **bill**. Bills are debated and voted on **the floor**.
A passed bill becomes an **Act**, numbered and published in the **gazette**.
Channels: #submit-a-bill (button + template), #the-floor (debate and
ballots), #gazette (the permanent record).

## The flow

### 1. Submitting a bill

- A pinned bot message in #submit-a-bill carries a button; clicking it
  opens a template (a Discord modal):
  - **What**: the text of the bill itself.
  - **Why**: the author's explanation of why it should pass.
- Both fields are required. A bill is not just a wish; it comes with
  reasoning attached.

### 2. Publication

- On submission, the bot publishes the bill to #the-floor.
- The post shows publicly: who authored it, the bill text, and their why.
- Bills are never anonymous. Authorship of laws is public record.
- (Skipped for now, revisit later: whether publication also sends an
  announcement.)

### 3. The debate chamber (debate layer)

- When a bill is published, the clerk creates a debate chamber: a category
  named for the bill, containing a text channel and a voice channel.
- The chamber has no owner or manager; the bill's author gets no special
  powers in it (a proposer-managed chamber was considered and rejected:
  it would create the server's first moderator).
- When the floor closes, the clerk locks the text channel and moves it to
  the archive category, readable forever: the house keeps its own history.
  The voice channel is deleted; voice is never recorded, so privacy in the
  chamber's VC is handled by physics, not policy.
- Stage channels considered and rejected; a plain VC is right at this size.

### 4. Notes (the record layer)

- Any member can attach notes to a bill, and each member gets exactly
  two slots per bill:
  - **one named note**, posted under their own name;
  - **one anonymous note**, posted with no attribution.
- The two-slot design is deliberate anonymity protection: because posting a
  named note never uses up the anonymous slot, you cannot deduce by
  elimination who wrote an anonymous note. Everyone always retains their
  anonymous slot regardless of what they said publicly.
- A member may use both slots on the same bill, and the two notes may
  even contradict each other. That is their business.
- Notes are editable by their author while the floor is open; changing
  your mind mid-debate is the point. Only the final version is preserved
  when the floor closes.
- Display order is strictly the order of first submission, and editing
  never moves a note. Position is purely chronological so nobody can read
  meaning into placement.
- Notes exist so voters can read the bill, the author's why, and the
  standing positions of others before deciding. Chat is for arguing;
  notes are for what you actually want remembered.

### 5. Voting

- Voting is one tap, as easy as possible (buttons, not reactions).
- Ballots are anonymous and never tracked. The bot counts votes but stores
  no record of who voted which way. Reactions are unusable here since they
  are always public; buttons are required.

## Implementation notes for later

- Discord mechanics that fit: slash command opens a modal (two text fields,
  what / why); bill posted to #the-floor as an embed; notes collected via
  a button that opens another modal (same modal to edit: the clerk updates
  the note in place, which preserves position); votes via buttons on
  the bill. Decided at build time: notes live in a member-proof thread
  attached to the bill on the floor (the floor's read-only overwrites
  cover threads, so the thread cannot contain chat); the buttons stay in
  the chamber; the thread is sealed (locked) at close.
- Debate chamber: category per active bill holding the text channel and
  VC; created on publication. At close: text channel locked (send denied
  for everyone) and moved into the archive category, VC deleted, bill's
  category removed. Notes live as clerk-posted embeds (in the chamber or
  under the floor post; decide at build time), with final versions
  preserved in the bill's record at close.
- Sprawl, two kinds: several bills at once means several categories
  (fine at this scale; a cap on simultaneous open floors is an easy lever),
  and the archive grows by one text channel per bill forever. Discord caps
  a server at 500 channels; at this group's likely pace that is years away,
  and consolidating old archives is the parliament's problem when it
  arrives.
- Anonymity is toward members, not toward the machine (decided July
  2026): the clerk keeps full records (who voted what, who wrote which
  anonymous note) so votes can be changed and notes edited. It simply
  never displays them to anyone. The Host can technically read the files;
  that is an accepted part of the trust model, consistent with the
  charter's known-limitations stance.
- Note slots imply per-bill per-member state: named-note-used,
  anon-note-used, has-voted.
- Launch rules are set by the Standing Orders (standing-orders.md),
  submitted as the first bill: only votes cast count, no quorum, more yes
  than no passes, ties fail, floor open [48 hours]. Deliberately fast so
  things move while the house is small; the first citizen this bothers
  can submit a quorum bill.
- Still undecided, for later: editing or withdrawing bills, whether notes
  can be deleted by their author. Per the charter, all values are the
  parliament's to change; the bot reads them from config, not hardcode.

## Design principles worth preserving

- Laws are public, ballots are secret. Same split as real parliaments.
- The anonymous note slot is a pressure valve: it lets someone say the
  awkward true thing without social cost, in a group small enough that
  every opinion is otherwise identifiable.
- Bills force articulation (the why field). Low-effort bills cost
  at least a paragraph of reasoning.
