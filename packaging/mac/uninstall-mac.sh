#!/bin/bash
set -euo pipefail

INSTALL_DIR="$HOME/vcoutreach"
WORKSPACE="$HOME/Documents/vcoutreach-workspace"
DRY_RUN=0
say() { printf '%s\n' "$*"; }
fail() { say "Error: $*" >&2; exit 1; }
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf 'Would run:'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}
while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --dir|--workspace)
            [ "$#" -ge 2 ] && [ -n "$2" ] || fail "$1 needs a value."
            case "$1" in
                --dir) INSTALL_DIR=$2 ;;
                --workspace) WORKSPACE=$2 ;;
            esac
            shift 2 ;;
        --help|-h)
            say 'Usage: bash uninstall-mac.sh [--dry-run] [--dir PATH] [--workspace PATH] [--help]'
            exit 0 ;;
        *) fail "Unknown option: $1. Use --help for options." ;;
    esac
done
[ "$(uname -s)" = Darwin ] || fail 'This uninstaller works only on macOS.'

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
for removal_target in "$INSTALL_DIR" "$WORKSPACE"; do
    guard_removal_directory "$removal_target"
    case "$DESKTOP/" in
        "$removal_target/"*) fail 'Refusing to remove a folder that contains your Desktop.' ;;
    esac
done
if [ -e "$INSTALL_DIR" ] || [ -L "$INSTALL_DIR" ] || [ "$DRY_RUN" -eq 0 ]; then
    [ -d "$INSTALL_DIR" ] && { [ -d "$INSTALL_DIR/.venv" ] || [ -f "$INSTALL_DIR/Outreach.command" ]; } ||
        fail 'The folder does not look like a vcoutreach install.'
fi
case "$WORKSPACE/" in
    "$INSTALL_DIR/"*) fail 'The install folder contains the workspace; choose separate folders.' ;;
esac
case "$INSTALL_DIR/" in
    "$WORKSPACE/"*) fail 'The workspace contains the install folder; choose separate folders.' ;;
esac

# Read confirmation before removing the script's own installation folder.
REMOVE_WORKSPACE=0
if [ "$DRY_RUN" -eq 1 ]; then
    say "Would ask on /dev/tty before deleting $WORKSPACE; the default is to keep it."
else
    answer=
    if { printf 'Also permanently remove workspace %s? [y/N] ' "$WORKSPACE" > /dev/tty &&
        IFS= read -r answer < /dev/tty; } 2>/dev/null; then
        case "$answer" in
            y|Y|yes|YES) REMOVE_WORKSPACE=1 ;;
        esac
    fi
fi
if [ -f "$DESKTOP/Outreach.command" ]; then
    if grep -q '^# vcoutreach launcher$' "$DESKTOP/Outreach.command"; then
        run rm -f -- "$DESKTOP/Outreach.command"
    else
        say "Keeping the unrecognized Desktop launcher at $DESKTOP/Outreach.command."
    fi
elif [ "$DRY_RUN" -eq 1 ]; then
    say "Would remove $DESKTOP/Outreach.command if it is a vcoutreach launcher."
fi
run rm -rf -- "$INSTALL_DIR"
if [ "$REMOVE_WORKSPACE" -eq 1 ]; then
    run rm -rf -- "$WORKSPACE"
    say "Removed the workspace at $WORKSPACE."
else
    say "Keeping your workspace at $WORKSPACE."
fi
if [ "$DRY_RUN" -eq 1 ]; then
    say 'Dry run complete; no files were changed.'
else
    say 'Removed vcoutreach; timestamped backups were kept.'
fi
