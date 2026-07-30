# Generation prompts by model

Same three assets, phrased for each model's prompt dialect. Palette is
constant everywhere: base #14161f, warm amber #e0a458, cyan #6ec3d9.
Lead variants: controller toast (icon), victory night (banner, approved),
owl counsel (avatar). Alternates live in the per-asset JSON files.

---

## Nano Banana (Gemini image)

Conversational prose, one paragraph, state the format up front.

**Icon (square, 1:1):**
Create a square 1024x1024 app icon in flat vector style with subtle paper
grain. Two game controllers clink together like champagne glasses in a
toast, with a small bright spark at the contact point and a few tiny
confetti flecks and popcorn pieces floating around them. Light the scene
with warm amber (#e0a458) from one side and cool cyan (#6ec3d9) from the
other, on a near-black charcoal navy background (#14161f). One bold
celebratory emblem, centered with generous margin, readable at 48 pixels.
No text, no letters, no hands, no watermark.

**Banner (16:9 landscape):**
Create a 1920x1080 landscape illustration in flat vector style with subtle
paper grain. A cozy living room at night at the exact second of a game
victory, seen from behind a big couch: three friends as dark silhouettes
leap with arms thrown up, one game controller flies mid-air, popcorn
scatters above the couch. The large TV erupts in an abstract victory burst
of cyan and white shapes (#6ec3d9). Warm amber fairy lights (#e0a458) hang
along the wall; an open pizza box and a toppled popcorn bowl sit on the
low table; a startled cat jumps off the armrest; a record player and
plants sit in the warm shadows. Deep charcoal navy palette (#14161f),
loud with joy. Keep the upper third darker and emptier. No text, no
visible faces, no watermark.

**Clerk avatar (square, 1:1):**
Create a square 1024x1024 character avatar in flat vector style with
subtle paper grain. A sleek, sharply groomed owl wearing a perfectly
tailored dark blazer, crisp white collar, and slim dark tie, with thin
elegant round spectacles, holding a leather document folio under one wing,
standing upright and confident behind a minimal modern lectern. One warm
amber accent light (#e0a458), one cool cyan rim light (#6ec3d9), near-black
charcoal navy background (#14161f). Composed and quietly formidable, the
round face large and readable at 40 pixels. Not an accountant: no visor,
no ledger, no banker's lamp. No text, no letters, no watermark.

---

## Midjourney (v7)

Fragment style, flags at the end.

**Icon:**
two game controllers clinking like a champagne toast, glow spark at
contact, tiny confetti and popcorn flecks, flat vector illustration,
subtle paper grain, warm amber light one side #e0a458, cyan light other
side #6ec3d9, near-black navy background #14161f, bold centered emblem,
app icon readability --ar 1:1 --v 7 --style raw --no text, letters,
watermark, hands, photo

**Banner:**
living room game night victory moment, view from behind couch, three dark
silhouettes leaping arms raised, controller mid-air, popcorn scattering,
TV bursting with abstract cyan victory shapes #6ec3d9, amber fairy lights
#e0a458, pizza box and toppled popcorn bowl, startled cat mid-jump,
record player and plants in shadow, flat vector illustration, subtle paper
grain, deep charcoal navy #14161f, joyful energy, dark upper third
--ar 16:9 --v 7 --style raw --no text, letters, watermark, faces, photo

**Clerk avatar:**
sleek owl in tailored dark blazer, crisp white collar, slim tie, thin
round spectacles, leather document folio under wing, upright behind
minimal lectern, sharp legal counsel energy, flat vector illustration,
subtle paper grain, amber accent light #e0a458, cyan rim light #6ec3d9,
near-black navy background #14161f, large readable face, avatar crop
--ar 1:1 --v 7 --style raw --no text, letters, watermark, visor, ledger,
lamp, photo

---

## GPT-image (ChatGPT / API)

Prose like Nano Banana; be extra explicit about text bans (it loves
adding labels) and ask for the exact size.

**Icon:** Use the Nano Banana icon prompt verbatim, and append: "Strictly
no typography of any kind anywhere in the image."

**Banner:** Use the Nano Banana banner prompt verbatim, same appended
sentence. Request 1792x1024 if 1920x1080 is unavailable, then upscale.

**Clerk avatar:** Use the Nano Banana avatar prompt verbatim, same
appended sentence.

---

## Flux / Stable Diffusion (SDXL)

Tag list + negative prompt.

**Icon:**
Prompt: flat vector illustration, subtle paper grain, two game
controllers clinking like a toast, glow spark at contact point, confetti
flecks, popcorn pieces, warm amber lighting #e0a458, cyan accent lighting
#6ec3d9, dark charcoal navy background #14161f, bold centered emblem, app
icon composition, generous margins, celebratory, high contrast silhouette
Negative: text, letters, typography, watermark, signature, hands, people,
photorealistic, 3d render, blur, clutter

**Banner:**
Prompt: flat vector illustration, subtle paper grain, cozy living room at
night, game victory moment, view from behind couch, three dark human
silhouettes jumping with raised arms, controller in mid-air, flying
popcorn, tv screen with abstract cyan victory burst #6ec3d9, string of
amber fairy lights #e0a458, pizza box, toppled popcorn bowl, startled cat
jumping, record player, potted plants, deep charcoal navy palette
#14161f, joyful, dynamic, dark empty upper third, 16:9
Negative: text, letters, typography, watermark, signature, visible faces,
photorealistic, 3d render, daylight, sterile, symmetrical

**Clerk avatar:**
Prompt: flat vector illustration, subtle paper grain, anthropomorphic owl
in tailored dark blazer, crisp white collar, slim dark tie, thin round
spectacles, leather document folio, minimal modern lectern, upright
confident pose, amber accent light #e0a458, cyan rim light #6ec3d9, dark
charcoal navy background #14161f, large readable face, avatar composition,
sharp, elegant
Negative: text, letters, typography, watermark, signature, visor, ledger,
banker lamp, accountant, photorealistic, 3d render, cute chibi, childish

---

## Universal acceptance checklist (any model)

- Reads instantly at thumbnail size (48px icon, 40px avatar).
- Two light temperatures present: amber and cyan.
- Mid-moment energy; reject anything calm, tidy, or catalog-like.
- Zero text or letters anywhere.
- Avatar must not read as an accountant, and must stay agender.
