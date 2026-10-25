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

Gmail: the easy sender setup is a Gmail app password (Settings, Gmail card). Google OAuth sign-in (Choose OAuth client file, then Connect Gmail) is optional and advanced.

Full documentation: [docs/mac-app.md](../../docs/mac-app.md). The older `install-mac.sh` developer installer is documented there too.
