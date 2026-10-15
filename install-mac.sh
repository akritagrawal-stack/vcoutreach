#!/bin/bash
set -euo pipefail

INSTALL_DIR="$HOME/vcoutreach"
WORKSPACE="$HOME/Documents/vcoutreach-workspace"
REF=main
DRY_RUN=0
DESKTOP_LAUNCHER=1
TEMP_DIR=
PYTHON=

say() { printf '%s\n' "$*"; }
fail() { say "Error: $*" >&2; exit 1; }
usage() {
    say 'Usage: bash install-mac.sh [--dry-run] [--dir PATH] [--workspace PATH] [--ref REF] [--no-launcher-on-desktop] [--help]'
}
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf 'Would run:'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}
cleanup() {
    if [ -n "$TEMP_DIR" ] && [ "$DRY_RUN" -eq 0 ]; then
        rm -rf -- "$TEMP_DIR"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --no-launcher-on-desktop) DESKTOP_LAUNCHER=0; shift ;;
        --dir|--workspace|--ref)
            [ "$#" -ge 2 ] && [ -n "$2" ] || fail "$1 needs a value."
            case "$1" in
                --dir) INSTALL_DIR=$2 ;;
                --workspace) WORKSPACE=$2 ;;
                --ref) REF=$2 ;;
            esac
            shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) fail "Unknown option: $1. Use --help for options." ;;
    esac
done

[ "$(uname -s)" = Darwin ] || fail 'This installer works only on macOS.'

# Resolve every component, including symlinks above paths that do not exist yet.
canonical_directory() {
    local pending=$1 resolved= component link links=0
    case "$pending" in
        '~/'*) pending="$HOME/${pending#\~/}" ;;
    esac
    case "$pending" in
        /*) ;;
        *) pending="$PWD/$pending" ;;
    esac
    while [ -n "$pending" ]; do
        component=${pending%%/*}
        if [ "$pending" = "$component" ]; then
            pending=
        else
            pending=${pending#*/}
        fi
        case "$component" in
            ''|.) continue ;;
            ..) resolved=${resolved%/*}; continue ;;
        esac
        if [ -L "$resolved/$component" ]; then
            links=$((links + 1))
            [ "$links" -le 40 ] || fail 'Cannot resolve a folder with a symbolic link loop.'
            link=$(readlink "$resolved/$component") || fail 'Cannot resolve a folder symbolic link.'
            case "$link" in
                /*) resolved= ;;
            esac
            pending="$link${pending:+/$pending}"
        else
            resolved="$resolved/$component"
        fi
    done
    printf '%s\n' "${resolved:-/}"
}

# Only specific app or workspace folders may be removed or moved aside.
guard_removal_directory() {
    local target=$1 broad
    case "$target" in
        /*/*/*) ;;
        *) fail 'Refusing to remove or move a system or broad directory.' ;;
    esac
    for broad in / /usr /Users /Applications /System /Library /bin /sbin /etc /var /opt /private /tmp /Volumes \
        "$HOME" "$HOME/Desktop" "$HOME/Documents" "$HOME/Downloads"; do
        broad=$(canonical_directory "$broad") || return 1
        [ "$target" != "$broad" ] || fail 'Refusing to remove or move a system or broad directory.'
    done
    case "$USER_HOME/" in
        "$target/"*) fail 'Refusing to remove or move your home folder or any of its parents.' ;;
    esac
}

INSTALL_DIR=$(canonical_directory "$INSTALL_DIR")
WORKSPACE=$(canonical_directory "$WORKSPACE")
USER_HOME=$(canonical_directory "$HOME")
DESKTOP=$(canonical_directory "$HOME/Desktop")
guard_removal_directory "$INSTALL_DIR"
[ "$INSTALL_DIR" != / ] && [ "$WORKSPACE" != / ] || fail 'Choose an app folder and workspace other than the filesystem root.'
case "$USER_HOME/" in
    "$INSTALL_DIR/"*) fail 'Choose an install folder inside your home folder, not your home folder or its parents.' ;;
esac
case "$INSTALL_DIR/" in
    "$WORKSPACE/"*) fail 'The install folder and workspace must be separate folders, with neither inside the other.' ;;
esac
case "$WORKSPACE/" in
    "$INSTALL_DIR/"*) fail 'The install folder and workspace must be separate folders, with neither inside the other.' ;;
esac
case "$DESKTOP/" in
    "$INSTALL_DIR/"*|"$WORKSPACE/"*) fail 'Choose folders that do not contain your Desktop.' ;;
esac

find_python() {
    local name candidate
    for name in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        for candidate in "$name" "/opt/homebrew/bin/$name" "/usr/local/bin/$name"; do
            if [ "$DRY_RUN" -eq 1 ]; then
                printf 'Would check: %q -c %q\n' "$candidate" 'import sys; sys.exit(sys.version_info < (3, 10))'
            fi
            if command -v "$candidate" >/dev/null 2>&1 &&
                "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
                PYTHON=$(command -v "$candidate")
                return 0
            fi
        done
    done
    return 1
}

if ! find_python; then
    BREW=
    for candidate in brew /opt/homebrew/bin/brew /usr/local/bin/brew; do
        if command -v "$candidate" >/dev/null 2>&1; then
            BREW=$(command -v "$candidate")
            break
        fi
    done
    if [ -n "$BREW" ]; then
        say 'Python 3.10 or newer is needed; Homebrew can install it.'
        if [ "$DRY_RUN" -eq 1 ]; then
            say 'Would ask on /dev/tty: Install Python with Homebrew now? [y/N]'
            run "$BREW" install python
            say 'Would search again for Python 3.10 or newer after installation.'
        else
            answer=
            if ! { printf 'Install Python with Homebrew now? [y/N] ' > /dev/tty &&
                IFS= read -r answer < /dev/tty; }; then
                fail 'Cannot read your answer; install Python from https://www.python.org/downloads/macos/ and try again.'
            fi
            case "$answer" in
                y|Y|yes|YES) "$BREW" install python ;;
                *) fail 'Install Python 3.10 or newer from https://www.python.org/downloads/macos/ and try again.' ;;
            esac
            find_python || fail 'Python is still unavailable; reopen Terminal and run the installer again.'
        fi
    elif [ "$DRY_RUN" -eq 1 ]; then
        say 'Would stop: install Python 3.10 or newer from https://www.python.org/downloads/macos/ first.'
    else
        fail 'Install Python 3.10 or newer from https://www.python.org/downloads/macos/ and try again.'
    fi
fi


if [ -n "$PYTHON" ]; then
    ENCODED_REF=$("$PYTHON" -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe="/"))' "$REF")
else
    PYTHON='<Python 3.10+>'
    ENCODED_REF=$REF
    say 'Would resolve folder paths and URL-encode the ref with Python once it is installed.'
fi
ARCHIVE_URL="https://github.com/akritagrawal-stack/vcoutreach/archive/refs/heads/$ENCODED_REF.tar.gz"
if [ "$DRY_RUN" -eq 1 ]; then
    TEMP_DIR='/tmp/vcoutreach.XXXXXX'
    say 'Would create a temporary folder with mktemp -d and clean it on exit.'
else
    TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/vcoutreach.XXXXXX")
fi
say "Downloading vcoutreach ($REF)."
run curl -fsSL "$ARCHIVE_URL" -o "$TEMP_DIR/source.tar.gz"
run mkdir -p "$TEMP_DIR/source"
run tar -xzf "$TEMP_DIR/source.tar.gz" --strip-components=1 -C "$TEMP_DIR/source"

backup_if_present() {
    local target=$1 backup suffix=0
    if [ -e "$target" ] || [ -L "$target" ]; then
        backup="$target.old-$(date +%Y%m%d-%H%M%S)"
        while [ -e "$backup" ] || [ -L "$backup" ]; do
            suffix=$((suffix + 1))
            backup="$target.old-$(date +%Y%m%d-%H%M%S)-$suffix"
        done
        say "Keeping the previous copy at $backup."
        run mv -- "$target" "$backup"
    elif [ "$DRY_RUN" -eq 1 ]; then
        say "Would preserve $target as a timestamped .old backup if it exists at installation time."
    fi
}

backup_if_present "$INSTALL_DIR"
run mkdir -p "$(dirname "$INSTALL_DIR")"
run mv -- "$TEMP_DIR/source" "$INSTALL_DIR"
say "Installing in $INSTALL_DIR."
run "$PYTHON" -m venv "$INSTALL_DIR/.venv"
run "$INSTALL_DIR/.venv/bin/python" -m pip --disable-pip-version-check install --quiet -e "$INSTALL_DIR"
run "$INSTALL_DIR/.venv/bin/vcoutreach" --version
run mkdir -p "$WORKSPACE"

# The single-quoted strings below belong to the generated launcher.
# shellcheck disable=SC2016
write_launcher() {
    printf '#!/bin/bash\n'
    printf '# vcoutreach launcher\n'
    printf 'set -euo pipefail\n'
    printf 'WORKSPACE=%q\n' "$WORKSPACE"
    printf 'cd -- "$WORKSPACE"\n'
    printf 'printf '\''Close this window to stop.\\n'\''\n'
    printf 'exec %q ui --workspace "$WORKSPACE"\n' "$INSTALL_DIR/.venv/bin/vcoutreach"
}
if [ "$DRY_RUN" -eq 1 ]; then
    say "Would write $INSTALL_DIR/Outreach.command with this content:"
    write_launcher
else
    write_launcher > "$INSTALL_DIR/Outreach.command"
fi
run chmod +x "$INSTALL_DIR/Outreach.command"
if [ "$DRY_RUN" -eq 1 ]; then
    say "Would remove com.apple.quarantine recursively from $INSTALL_DIR (ignore missing attributes)."
else
    xattr -dr com.apple.quarantine "$INSTALL_DIR" 2>/dev/null || true
fi
if [ "$DESKTOP_LAUNCHER" -eq 1 ]; then
    run mkdir -p "$DESKTOP"
    backup_if_present "$DESKTOP/Outreach.command"
    run cp -- "$INSTALL_DIR/Outreach.command" "$DESKTOP/Outreach.command"
    if [ "$DRY_RUN" -eq 1 ]; then
        say "Would remove com.apple.quarantine from $DESKTOP/Outreach.command (ignore missing attributes)."
    else
        xattr -d com.apple.quarantine "$DESKTOP/Outreach.command" 2>/dev/null || true
    fi
fi
if [ "$DRY_RUN" -eq 1 ]; then
    say 'Dry run complete; no files were changed and no network requests were made.'
else
    say "Installed vcoutreach in $INSTALL_DIR; workspace: $WORKSPACE."
fi
if [ "$DESKTOP_LAUNCHER" -eq 1 ]; then
    say 'Open it by double-clicking Outreach.command on your Desktop.'
else
    say "Open it by double-clicking $INSTALL_DIR/Outreach.command."
fi
say 'Update by re-running the same installer line (include your custom flags if used):'
say 'curl -fsSL https://raw.githubusercontent.com/akritagrawal-stack/vcoutreach/main/install-mac.sh | bash'
printf 'Remove the app and workspace: rm -rf -- %q %q\n' "$INSTALL_DIR" "$WORKSPACE"
printf 'Remove the Desktop shortcut: rm -f -- %q\n' "$DESKTOP/Outreach.command"
say 'To keep your workspace, use packaging/mac/uninstall-mac.sh inside the install folder.'
