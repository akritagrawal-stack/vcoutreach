# Publikreach.app

Native macOS app (Cocoa WKWebView window hosting the Flask app, bundled Python, no install needed on the target Mac). Apple Silicon (arm64) only.

Build on a real Mac:

```sh
bash packaging/mac/build_app.sh
```

Outputs `dist/Publikreach.app` and `dist/Publikreach.dmg`. Headless check:

```sh
dist/Publikreach.app/Contents/MacOS/Publikreach --self-test
```

Install: `curl -fsSL https://raw.githubusercontent.com/akritagrawal-stack/vcoutreach/main/packaging/install.sh | bash` (no Gatekeeper prompt). For a browser-downloaded dmg, right-click the app and choose Open once, or run `xattr -cr /Applications/Publikreach.app`. The app is ad hoc signed and open source.

The app's workspace is `~/Documents/Publikreach` and it starts in TEST mode every launch.

Gmail: connect with your Google OAuth client file (Settings, Gmail card): download your OAuth client file, choose it with Choose OAuth client file, then click Connect Gmail. The simpler alternative, if you do not want a Google Cloud project, is a Gmail app password, under "Alternative: use a Gmail app password instead" on the Gmail card (about two minutes).

SearXNG web-search enrichment is optional, default http://localhost:8080. Start it with: `docker compose -f packaging/searxng/docker-compose.yml up -d`.

Full documentation: [docs/mac-app.md](../../docs/mac-app.md). The older `install-mac.sh` developer installer is documented there too.
