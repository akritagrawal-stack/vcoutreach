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

First launch: the app is ad-hoc signed, not notarized. Right-click the app and choose Open. Its workspace is `~/Documents/Publikreach` and it starts in TEST mode every launch.

Gmail needs your own Google Cloud Desktop app OAuth client JSON (Settings > Choose OAuth client file > Connect Gmail).

Full documentation: [docs/mac-app.md](../../docs/mac-app.md). The older `install-mac.sh` developer installer is documented there too.
