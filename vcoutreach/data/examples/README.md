# Example data

`Publik_Pitch_Deck_Example.pdf` is the Publik pitch deck with the personal info
redacted: the founder profile slide is included with both photos, both names and every
bio bullet painted over, and the round and audience figures are replaced by placeholders. It is image-only (no text layer, no links)
and is shipped so a fresh workspace has a working attachment. On first run the app copies it to
`attachments/Example_Pitch_Deck.pdf` inside the workspace and shows the Pitch deck tile
as Ready. Replace it with your own PDF in Settings (Local pitch deck path) before you
send real email.

Rebuild it from the full deck with `packaging/tools/make_example_deck.py SOURCE_PDF`.
The cover headline stays "F*ck software subscriptions." exactly as in the original deck
(owner request, 2026-10-26). Do not reword or repaint it.
