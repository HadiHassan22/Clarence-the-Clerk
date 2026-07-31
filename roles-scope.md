# Member Roles Scope

Status: scoped, not started. Phase 5 in ROADMAP.md.

## The feature

- Any member can create up to 5 roles of their own through the bot
  (N=5 decided July 2026).
- A role is a name plus any color. Nothing else: zero permissions,
  purely cosmetic identity. The color can be said in words ("sea green")
  or given as hex; nobody is required to know hex to take part.
- A name already in use is refused, whoever holds it. Two roles sharing a
  name are indistinguishable to everyone looking at them.
- Creators can rename, recolor, and delete their own roles.
- Between the roles they created, members can change the priority ordering
  (which determines which color wins when someone wears several).

## The unstated purpose

The feature exists to let political parties emerge naturally: shared
colors, shared banners under a name. This purpose is deliberately never
mentioned anywhere user-facing. The feature is presented as custom
colors/flair, nothing more. If parties form, they were not suggested;
they were merely possible. This framing is a design requirement, not a
nicety: discovery is the point.

## Discord constraint to design around

Role order (which decides displayed color) is global to the server, not
per-member. If someone wears two roles, the higher role in the server-wide
list wins for everyone wearing that pair. There is no native per-member
"show this color" choice. So "creators reorder their own roles" moves
those roles in the global list, which affects every wearer. The bot will
need a policy here; options when we get to it:

- creator-controlled ordering, global effect (simplest, matches the scope);
- one-role-per-member worn at a time (sidesteps ordering entirely);
- member picks a "display role" and the bot enforces it by juggling
  assignments rather than order.

## Open questions for later

- Can members join roles created by others, and is joining open or does
  the creator control membership? (Parties need joinable roles; this is
  probably "open to all", but unconfirmed.)
- What is [N], the per-member creation limit?
- Can a role be transferred to another owner? What happens to a role when
  its creator leaves?
- Are role names subject to any rule at all, or is that the parliament's
  problem the first time someone abuses it?

## Implementation notes

- Bot commands: create / edit / delete / reorder, plus join/leave if roles
  are joinable.
- The bot keeps a registry mapping each custom role to its creator (needed
  for edit rights and the creation limit). This registry is part of the
  off-server backup.
- All bot-created roles sit below the bot's own role and carry no
  permissions, so the feature cannot touch governance.
