# Example data

`Publik_Pitch_Deck_Example.pdf` is the Publik pitch deck with the founder, round
and audience-stat pages removed. It is image-only (no text layer, no links) and is
shipped so a fresh workspace has a working attachment. On first run the app copies it to
`attachments/Example_Pitch_Deck.pdf` inside the workspace and shows the Pitch deck tile
as Ready. Replace it with your own PDF in Settings (Local pitch deck path) before you
send real email.

Rebuild it from the full deck with `packaging/tools/make_example_deck.py SOURCE_PDF`.
After a rebuild, repaint the cover headline as "Skip software subscriptions." (same
typeface and colors) so the example carries no profanity.
