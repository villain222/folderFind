#!/usr/bin/env bash
# Installs the "ff" fuzzy folder finder for the current user.
#
#   ./install.sh                 # install to ~/.local/bin and wire up your shell
#   FOLDERFIND_BIN=/usr/local/bin ./install.sh
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/ff.py"
dest="${FOLDERFIND_BIN:-$HOME/.local/bin}"

if [ ! -f "$src" ]; then
    echo "error: $src not found" >&2
    exit 1
fi

find_python() {
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
                command -v "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

python_bin="$(find_python)" || {
    echo "error: Python 3.8 or newer was not found" >&2
    exit 1
}

escape_single() {
    printf '%s' "$1" | sed "s/'/'\\\\''/g"
}

mkdir -p "$dest"
cp "$src" "$dest/ff.py"
chmod 755 "$dest/ff.py"

# Small wrapper so the shebang/interpreter that was verified above is used.
cat > "$dest/ff" <<EOF
#!/bin/sh
exec '$(escape_single "$python_bin")' '$(escape_single "$dest/ff.py")' "\$@"
EOF
chmod 755 "$dest/ff"
echo "Installed $dest/ff"

# ---------------------------------------------------------------------------
# shell integration: a function is required because a program cannot change
# its parent shell's working directory.
# ---------------------------------------------------------------------------
shell_name="$(basename "${SHELL:-bash}")"
case "$shell_name" in
    zsh|bash|fish) ;;
    *) shell_name="bash" ;;
esac

case "$shell_name" in
    zsh)  rc="$HOME/.zshrc" ;;
    bash)
        if [ "$(uname -s)" = "Darwin" ] && [ -f "$HOME/.bash_profile" ]; then
            rc="$HOME/.bash_profile"
        else
            rc="$HOME/.bashrc"
        fi
        ;;
    fish) rc="$HOME/.config/fish/config.fish" ;;
esac

mkdir -p "$(dirname "$rc")"
touch "$rc"

snippet="$(FOLDERFIND_LAUNCHER="$dest/ff" "$dest/ff" --init "$shell_name")"

if [ "$shell_name" = "fish" ]; then
    path_line="fish_add_path \"$dest\""
else
    path_line="export PATH=\"$dest:\$PATH\""
fi

tmp="$(mktemp "${TMPDIR:-/tmp}/folderfind.XXXXXX")"
awk '
    /^# >>> folderfind >>>$/ { skip = 1; next }
    /^# <<< folderfind <<<$/ { skip = 0; next }
    !skip { print }
' "$rc" > "$tmp"

{
    cat "$tmp"
    printf '\n# >>> folderfind >>>\n'
    case ":$PATH:" in
        *":$dest:"*) ;;
        *) printf '%s\n' "$path_line" ;;
    esac
    printf '%s\n' "$snippet"
    printf '# <<< folderfind <<<\n'
} > "$rc"
rm -f "$tmp"

echo "Added the ff function to $rc"
echo ""
echo "Done. Next steps:"
echo "  1. Restart your shell, or run: . \"$rc\""
echo "  2. ff -u          # build the folder index"
echo "  3. ff ai proj     # pick a folder and cd into it"

case ":$PATH:" in
    *":$dest:"*) ;;
    *)
        echo ""
        echo "note: $dest is not on PATH yet; the profile line above adds it"
        ;;
esac
