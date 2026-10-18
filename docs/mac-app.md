# Publikreach.app

The end-user deliverable is `dist/Publikreach.dmg`. Open it, drag **Publikreach.app** onto **Applications**, eject the image, and double-click the app. The target Mac needs no Python installation. This is a native Cocoa WKWebView window hosting the existing Flask application, with a bundled Python runtime, Flask and Gmail libraries. The source checkout does not contain a prebuilt release or promise an available download.

Apple Silicon (arm64) only. The bundle declares macOS 12.0 as its minimum and supports high resolution displays. Actual macOS 12 compatibility still requires a run on that OS: the build host's Python and binary wheels must also support it. Intel Macs are not supported by this build.

## First launch and signing

The build uses `codesign --force --deep -s -`: an ad-hoc signature without an Apple Developer ID. The app is not notarized. On first launch, right-click Publikreach.app and choose Open. If macOS still blocks it, attempt to open it once, then go to System Settings > Privacy and Security > Open Anyway. Older macOS versions use System Preferences > Security and Privacy. Exact wording varies with macOS version and policy.

Notarization requires a paid Apple Developer account and signing credentials. It has not been done, and this build does not claim Gatekeeper approval.

The app creates `~/Documents/Publikreach`, with `.env`, `data/`, `drafts/` and `outputs/`. Allow access to Documents if macOS requests it. Replacing the app does not remove this workspace. To uninstall, quit and move the app to Trash; remove the workspace separately only if you want to delete its data and credentials.

## Gmail setup

Every user needs their own Google Cloud **Desktop app** OAuth client JSON until a shared client is bundled. Follow Google's [Python Gmail quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python), including enabling the Gmail API, configuring the consent screen and test users, and [creating a Desktop OAuth client](https://developers.google.com/workspace/gmail/api/quickstart/python#authorize_credentials_for_a_desktop_application).

1. In Settings, click **Choose OAuth client file** and select the downloaded JSON using the native dialog.
2. Click **Connect Gmail**. Complete consent in the system browser, then return to the app. Consent expires locally after three minutes if left unfinished.
3. Settings shows connected when a local refresh token is saved. Gmail access is verified when used. Consent permits `gmail.modify` for reading, labeling, drafting and sending; the app's existing send gates still apply.

The app validates the Desktop client format, nonempty client_id and client_secret, Google's authorization and token endpoints, and loopback redirect URIs. Web clients, service accounts, token files and endpoint substitutions are rejected. The JSON is copied server-side to `data/.gmail_credentials.json`; the token is saved at `data/.gmail_token.json`. Both files have mode 0600. Their contents are never shown or logged. Replacing a client invalidates the old saved token and requires consent again.

The same Settings actions work in `vcoutreach ui`: the browser file chooser uploads through an authenticated, same-origin, CSRF-protected route. Connecting uses the same installed-app OAuth flow. The CLI remains available as `vcoutreach auth gmail-web --workspace DIR` after saving the client JSON in that workspace's data folder.

## Build on a real Mac

Use a native arm64 shell and Python 3.10 through 3.14 with venv support. The target Mac does not need Python; this requirement is only for the build host. Building needs internet access for pip, Xcode command line tools for codesign, and macOS hdiutil.

```sh
bash packaging/mac/build_app.sh
```

Set `PYTHON=python3.13` if needed. The script creates a temporary virtual environment, installs the repo with extras `web,gmail`, pywebview and PyInstaller, regenerates the wordmark ICNS, bundles the application, signs and verifies it, runs its headless check, and creates a compressed DMG with an Applications symlink for drag installation. Temporary build files are cleaned on exit. Existing build outputs in dist are replaced. No git operations run.

Outputs:

- `dist/Publikreach.app`
- `dist/Publikreach.dmg`

The spec includes the web templates, static assets, samples, schema.sql, migrations, scripts, Google API discovery data and dynamically imported application modules. Optional provider integrations beyond Flask and Gmail still need their dependencies added to the build if distributed. No private workspace or OAuth client is bundled. Info.plist includes bundle id `com.publik.reach`, minimum system version, high resolution support and single-instance preference. There is no NSAppTransportSecurity exception.

## Headless verification and helper imports

On a machine allowed to bind loopback sockets, run either command:

```sh
python3 packaging/mac/app/app_main.py --self-test
"dist/Publikreach.app/Contents/MacOS/Publikreach" --self-test
```

The check uses a temporary workspace by default, starts the actual server, checks /healthz and authenticated pages and static assets, verifies unauthenticated access is refused, then stops the server. Exit code 0 means the check passed. `--workspace DIR` uses a specified workspace and resets it to TEST mode. It never imports or launches pywebview in self-test mode. These checks need loopback sockets and cannot run in a socket-denied sandbox.

For a separate test author, add `packaging/mac/app` to sys.path and import `runtime` or `app_main`. Imports do not start a window or server. `runtime` exposes `bootstrap_workspace`, `pick_free_port` (injectable socket factory), `SingleInstanceLock`, `exchange_token`, `start_server` (injectable server factory), and `self_test`. Shared OAuth helpers are in `vcoutreach.web.gmail.setup`: `validate_oauth_client`, `store_oauth_client` and `copy_oauth_client`. All file helpers accept an explicit workspace. Flask's test client can exercise token exchange and Gmail routes without binding a socket.

The desktop server binds port zero directly on 127.0.0.1 and retains the selected socket, avoiding a free-port probe race. It keeps Host, cookie and CSRF guards. Startup exchanges the one-use access token inside Python and installs the resulting HttpOnly, SameSite=Strict cookie in WKWebView's private cookie store before loading a clean local URL. No token link is shown or opened in a browser. The native bridge uses static closures and direct script execution instead of pywebview's generated Function and eval wrappers, preserving the web CSP. A file lock prevents a second app process from resetting the running workspace; a duplicate launch exits. The lock releases on exit or crash. Closing the window shuts down the HTTP server and scheduler.

Before distributing, verify on a real Mac: build, headless check, Finder launch, native window and title, Gmail file chooser and consent, duplicate launch, close and relaunch in TEST mode, Documents access, and first-run Gatekeeper behavior from a downloaded DMG. Native cookie installation and the JS bridge must be checked with the unchanged CSP. The headless check alone does not verify GUI behavior, OAuth consent, signing acceptance or minimum-OS compatibility.

Implementation references: [pywebview API](https://pywebview.flowrl.com/api/), [PyInstaller macOS architecture notes](https://pyinstaller.org/en/stable/feature-notes.html), and [PyInstaller spec files](https://pyinstaller.org/en/stable/spec-files.html).

## Developer option: install-mac.sh

This older option installs Python sources and a browser launcher. It is useful for development and does not produce the standalone native app.

```sh
curl -fsSL https://raw.githubusercontent.com/akritagrawal-stack/vcoutreach/main/install-mac.sh | bash
```

It installs `.[web,gmail]` in ~/vcoutreach using Python 3.10 or newer and creates Desktop/Outreach.command. The launcher opens the browser version in TEST mode. Its workspace remains ~/Documents/vcoutreach-workspace. Close its Terminal window to stop it. Re-running updates sources with .old backups. Options: --dry-run, --dir PATH, --workspace PATH, --ref REF, --no-launcher-on-desktop, --help. Optional integrations require a separate install of the integrations extra.

Uninstall that developer installation using `bash "$HOME/vcoutreach/packaging/mac/uninstall-mac.sh"`. It keeps the workspace by default and asks before deleting it. This uninstaller does not remove the new standalone app or its separate workspace.
