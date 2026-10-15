# Install on a Mac

Paste this one line into Terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/akritagrawal-stack/vcoutreach/main/install-mac.sh | bash
```

The installer downloads the app, finds Python 3.10 or newer, and installs it in ~/vcoutreach.
If needed, it offers to install Python with an existing Homebrew installation.
Your contacts and drafts stay in ~/Documents/vcoutreach-workspace.
Double-click Outreach.command on your Desktop to open the app in your browser.
Close the Terminal window to stop the app.
To update, run the same line again. Previous installs get timestamped .old backups.
Options: --dry-run, --dir PATH, --workspace PATH, --ref REF, --no-launcher-on-desktop, --help.
With options, end the installer line with `| bash -s -- --dir "$HOME/My Outreach"`.
The --ref option selects a GitHub branch (default: main).

- **Python too old:** Install a current Python from https://www.python.org/downloads/macos/ and retry.
- **Permission denied:** Choose a folder you own with --dir; do not use sudo.
  If the launcher is not executable, run `chmod +x "$HOME/Desktop/Outreach.command"`.
- **Cannot be opened because it is from an unidentified developer:** Right-click Outreach.command, choose Open, then confirm Open.

To uninstall, run `bash "$HOME/vcoutreach/packaging/mac/uninstall-mac.sh"`.
It removes the app and Desktop launcher, asks before deleting the workspace, and keeps it by default. Use --dir and --workspace for custom folders.
