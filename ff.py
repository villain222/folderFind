#!/usr/bin/env python3
"""ff - fuzzy folder finder.

Searches a cached index of directories for fuzzy matches of the words you
give it, shows the best matches, and prints the one you pick so a shell
wrapper can `cd` there.  (A child process cannot change its parent's working
directory, which is why the shell integration exists.)

Quick help:
    ff                     list folders in the current directory and pick one
    ff proj ai             fuzzy-match "proj" and "ai" anywhere in a path
    ff -u                  rebuild the folder index
    ff -n 20 react         show up to 20 matches
    ff --init powershell   print the shell integration function
    ff --stats             show index location and size

See README.md for install and configuration details.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections import deque

APP = "folderfind"
VERSION = "1.0.0"
IS_WINDOWS = os.name == "nt"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# Characters that make a match "word aligned" and therefore score higher.
SEPARATORS = frozenset("/\\_- .@+()[]{},$#~")

# Always skipped while indexing.  Add more names in ~/.folderfind.json.
DEFAULT_EXCLUDES = [
    ".git", ".hg", ".svn",
    "node_modules", "bower_components", "jspm_packages",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", ".tox", ".nox", "site-packages", "dist-packages",
    ".next", ".nuxt", ".parcel-cache", ".turbo", ".gradle", ".cargo",
    "$Recycle.Bin", "System Volume Information", "WinSxS", "Windows.old",
    "AppData", "Temp", "tmp",
]

DEFAULT_CONFIG = {
    "roots": None,          # None -> home + non-system fixed drives
    "exclude": [],          # extra directory names to skip
    "max_depth": 10,
    "max_entries": 250000,
    "results": 10,
    "stale_days": 7,
}

RESULT_COLOR = "\x1b[1;36m"
ORANGE_COLOR = "\x1b[38;5;208m"
DIM_COLOR = "\x1b[2m"
RESET_COLOR = "\x1b[0m"
REFINE = object()   # sentinel returned by the picker


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def path_basename(path):
    """basename that understands both / and \\ (useful in tests too)."""
    return path[max(path.rfind("/"), path.rfind("\\")) + 1:]


def display_path(path):
    """Contract the home directory to ~ for nicer output."""
    home = os.path.expanduser("~")
    if home and home not in ("/", "\\"):
        low_path, low_home = path.lower(), home.lower()
        if low_path == low_home:
            return "~"
        if low_path.startswith(low_home) and len(path) > len(home):
            if path[len(home)] in "/\\":
                return "~" + path[len(home):]
    return path


def ps_quote(text):
    return "'" + text.replace("'", "''") + "'"


def setup_streams():
    """Never crash on a legacy console because of an odd character."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def enable_ansi(stream):
    """Turn on VT sequences on Windows consoles; report whether color is ok."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if not IS_WINDOWS:
        return True
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-12 if stream is sys.stderr else -11)
        mode = ctypes.c_uint()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            k32.SetConsoleMode(handle, mode.value | 0x0004)
            return True
    except Exception:
        pass
    return False


def terminal_width(default=100):
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except OSError:
        return default


# --------------------------------------------------------------------------
# fuzzy matching
# --------------------------------------------------------------------------

def alignment_score(token, path, low, positions):
    """Score one concrete alignment of a token inside a path."""
    score = 0.0
    prev = -2
    for qi, j in enumerate(positions):
        if j == 0:
            score += 16.0
        elif low[j - 1] in SEPARATORS:
            score += 10.0
        if path[j].isupper() and token[qi].islower():
            score += 1.5          # camelCase hit
        gap = j - prev - 1
        if prev >= 0:
            if gap == 0:
                score += 7.0
            elif gap <= 2:
                score += 2.0 - gap
            else:
                score -= min(gap, 30) * 0.4
        prev = j
    score -= positions[0] * 0.15   # earlier start is better
    return score


def token_score(token, path, low):
    """Score one search token against a path.

    The token must appear as a subsequence of the path.  Positions at the
    start of the string or after a separator score best, adjacent characters
    score best, and large gaps are penalised.

    When the first character also occurs at a word boundary a little later
    (e.g. the "p" of "app" vs the "p" of "projects"), both alignments are
    tried and the better one wins.

    Returns (score, positions) or None.
    """
    first = token[0]
    earliest = low.find(first)
    if earliest < 0:
        return None
    starts = [earliest]
    if earliest > 0 and low[earliest - 1] not in SEPARATORS:
        window_end = min(earliest + 40, len(low))
        k = low.find(first, earliest + 1, window_end)
        while k >= 0 and len(starts) < 6:
            if low[k - 1] in SEPARATORS:
                starts.append(k)
            k = low.find(first, k + 1, window_end)

    best = None
    for start in starts:
        positions = [start]
        cursor = start + 1
        for ch in token[1:]:
            j = low.find(ch, cursor)
            if j < 0:
                positions = None
                break
            positions.append(j)
            cursor = j + 1
        if not positions:
            continue
        score = alignment_score(token, path, low, positions)
        if best is None or score > best[0]:
            best = (score, positions)
    return best


def score_path(path, tokens, cwd=None):
    """Score all tokens against one path.  Returns (score, positions) or None."""
    low = path.lower()
    base = path_basename(path).lower()
    total = 0.0
    positions = []
    for token in tokens:
        matched = token_score(token, path, low)
        if matched is None:
            return None
        token_score_value, token_positions = matched
        total += token_score_value
        positions.extend(token_positions)
        if token in base:
            total += 18.0
            if base.startswith(token):
                total += 20.0
            if base == token:
                total += 80.0
    total -= len(path) * 0.02
    total -= path.count(os.sep) * 0.6
    if base.startswith("."):
        total -= 3.0
    if cwd:
        cwd_prefix = cwd.rstrip("/\\").replace("\\", "/").lower() + "/"
        if low.replace("\\", "/").startswith(cwd_prefix):
            total += 15.0          # prefer folders near where you already are
    return total, sorted(set(positions))


class Match:
    __slots__ = ("path", "score", "positions")

    def __init__(self, path, score=0.0, positions=()):
        self.path = path
        self.score = score
        self.positions = list(positions)


def search(dirs, term, limit, cwd=None):
    """Return (matches, total_match_count) for a search term."""
    tokens = [t.lower() for t in term.split() if t]
    if not tokens:
        return [], 0
    found = []
    for path in dirs:
        scored = score_path(path, tokens, cwd)
        if scored is not None:
            score, positions = scored
            found.append(Match(path, score, positions))
    found.sort(key=lambda m: (-m.score, len(m.path), m.path.lower()))
    total = len(found)
    if limit:
        found = found[:limit]
    return found, total


def local_dirs(limit=None):
    """Folders directly inside the current directory (used with no search term)."""
    cwd = os.getcwd()
    names = []
    try:
        with os.scandir(cwd) as it:
            for entry in it:
                try:
                    if entry.is_dir():
                        names.append(entry.name)
                except OSError:
                    continue
    except OSError:
        return [], 0
    names.sort(key=str.lower)
    total = len(names)
    if limit:
        names = names[:limit]
    return [Match(os.path.join(cwd, name)) for name in names], total


# --------------------------------------------------------------------------
# index
# --------------------------------------------------------------------------

def config_path():
    override = os.environ.get("FOLDERFIND_CONFIG")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), "." + APP + ".json")


def cache_dir():
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(
            os.path.join("~", "AppData", "Local"))
        # The installer puts the program in %LOCALAPPDATA%\folderFind, so keep
        # the index in a subfolder: program files and cache must never be
        # deleted together by accident.
        return os.path.join(base, APP, "cache")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return os.path.join(base, APP)


def index_path():
    return os.path.join(cache_dir(), "index.txt")


def meta_path():
    return os.path.join(cache_dir(), "index.json")


def load_config(override=None):
    cfg = dict(DEFAULT_CONFIG)
    cfg["exclude"] = list(DEFAULT_EXCLUDES)
    path = os.path.abspath(os.path.expanduser(override)) if override else config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        print("warning: could not read %s: %s" % (path, exc), file=sys.stderr)
        data = {}
    if isinstance(data, dict):
        extra_excludes = data.get("exclude")
        if isinstance(extra_excludes, list):
            cfg["exclude"].extend(str(x) for x in extra_excludes)
        roots = data.get("roots")
        if isinstance(roots, list) and roots:
            cfg["roots"] = [str(r) for r in roots]
        for key in ("max_depth", "max_entries", "results", "stale_days"):
            value = data.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                cfg[key] = value
    if not cfg["roots"]:
        cfg["roots"] = default_roots()
    expanded = []
    for root in cfg["roots"]:
        root = os.path.abspath(os.path.expandvars(os.path.expanduser(str(root))))
        if root not in expanded:
            expanded.append(root)
    cfg["roots"] = expanded
    cfg["config_path"] = path
    return cfg


def fixed_drives():
    """Fixed local drives on Windows, e.g. ['C:\\', 'D:\\']."""
    if not IS_WINDOWS:
        return []
    try:
        import ctypes

        get_type = ctypes.windll.kernel32.GetDriveTypeW
        get_type.argtypes = [ctypes.c_wchar_p]
        get_type.restype = ctypes.c_uint
        drives = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            root = letter + ":\\"
            if get_type(root) == 3:  # DRIVE_FIXED
                drives.append(root)
        return drives
    except Exception:
        return []


def default_roots():
    """Home, plus any fixed drive that is not the system drive."""
    roots = [os.path.abspath(os.path.expanduser("~"))]
    system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\").upper()
    for drive in fixed_drives():
        if drive.rstrip("\\").upper() != system_drive:
            roots.append(drive)
    return roots


def volume_key(path):
    """Case-insensitive key for de-duplicating roots."""
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


def is_under(path, parent):
    path_key = volume_key(path).lower()
    parent_key = volume_key(parent).lower()
    return path_key == parent_key or path_key.startswith(parent_key + os.sep)


def prune_roots(roots):
    result = []
    for root in sorted(roots, key=len):
        if not any(is_under(root, kept) for kept in result):
            result.append(root)
    return result


def build_index(roots, excludes, max_depth, max_entries, progress=None):
    """Walk the roots and return (directories, truncated)."""
    skip = {str(name).lower() for name in excludes}
    found = []
    truncated = False
    queue = deque()
    seen = set()
    for root in roots:
        root = os.path.abspath(os.path.expanduser(str(root)))
        key = volume_key(root)
        if key in seen or not os.path.isdir(root):
            continue
        seen.add(key)
        found.append(root)
        queue.append((root, 0))

    while queue:
        if len(found) >= max_entries:
            truncated = True
            break
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink() or entry.name.lower() in skip:
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if IS_WINDOWS:
                    is_junction = getattr(entry, "is_junction", None)
                    if is_junction is not None and is_junction():
                        continue
                    attrs = entry.stat(follow_symlinks=False).st_file_attributes
                    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                        continue
            except OSError:
                continue
            child = os.path.join(current, entry.name)
            found.append(child)
            if progress is not None and len(found) % 5000 == 0:
                progress(len(found))
            if len(found) >= max_entries:
                truncated = True
                break
            queue.append((child, depth + 1))
    return found, truncated


def save_index(dirs, roots, truncated):
    directory = cache_dir()
    os.makedirs(directory, exist_ok=True)
    tmp = index_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for path in dirs:
            fh.write(path)
            fh.write("\n")
    os.replace(tmp, index_path())
    meta = {
        "version": 1,
        "count": len(dirs),
        "built": time.time(),
        "roots": roots,
        "truncated": bool(truncated),
        "platform": sys.platform,
    }
    tmp_meta = meta_path() + ".tmp"
    with open(tmp_meta, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    os.replace(tmp_meta, meta_path())


def load_meta():
    try:
        with open(meta_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_index():
    try:
        with open(index_path(), "r", encoding="utf-8", errors="replace") as fh:
            return [line.rstrip("\n") for line in fh if line.strip()]
    except OSError:
        return []


def rebuild_index(cfg, extra_roots=(), depth=None, quiet=False):
    roots = list(cfg["roots"]) + [
        os.path.abspath(os.path.expanduser(r)) for r in extra_roots]
    roots = prune_roots(roots)
    max_depth = depth or cfg["max_depth"]
    if not quiet:
        print("indexing: %s" % ", ".join(roots), file=sys.stderr)
        print("  (max depth %d, up to %s folders)" % (
            max_depth, "{:,}".format(cfg["max_entries"])), file=sys.stderr)
    start = time.time()

    def progress(count):
        if sys.stderr.isatty():
            sys.stderr.write("\r  scanned %s folders..." % "{:,}".format(count))
            sys.stderr.flush()

    dirs, truncated = build_index(
        roots, cfg["exclude"], max_depth, cfg["max_entries"], progress)
    if sys.stderr.isatty() and not quiet:
        sys.stderr.write("\r" + " " * 60 + "\r")
    try:
        save_index(dirs, roots, truncated)
    except OSError as exc:
        print("error: could not write the index: %s" % exc, file=sys.stderr)
        return dirs
    if not quiet:
        elapsed = time.time() - start
        message = "indexed %s folders in %.1fs" % ("{:,}".format(len(dirs)), elapsed)
        if truncated:
            message += " (hit max_entries; raise it in %s)" % config_path()
        print(message, file=sys.stderr)
    return dirs


def ensure_index(cfg):
    meta = load_meta()
    if os.path.exists(index_path()) and "built" in meta:
        age_days = (time.time() - float(meta.get("built", 0))) / 86400.0
        if age_days > cfg["stale_days"]:
            print("note: folder index is %.0f days old - run 'ff -u' to refresh"
                  % age_days, file=sys.stderr)
        return load_index(), meta
    if not sys.stdin.isatty() and not sys.stderr.isatty():
        # Non-interactive callers should not sit through a long first scan.
        print("error: no index yet; run 'ff -u' once", file=sys.stderr)
        return [], {}
    print("building the folder index (first run only)...", file=sys.stderr)
    return rebuild_index(cfg), load_meta()


# --------------------------------------------------------------------------
# presentation + picker
# --------------------------------------------------------------------------

def fit_path(text, positions, width):
    """Shorten a path to `width` chars, keeping matched positions aligned."""
    width = max(int(width), 12)
    if len(text) <= width:
        return text, list(positions)
    start = len(text) - (width - 1)
    if positions:
        first = positions[0]
        if first < start:
            start = max(0, min(first - 3, len(text) - (width - 1)))
    prefix = "..." if start > 0 else ""
    end = min(len(text), start + width - len(prefix))
    chunk = text[start:end]
    shifted = [p - start + len(prefix) for p in positions
               if start <= p < end]
    return prefix + chunk, shifted


def colorize(text, positions, color):
    if not color or not positions:
        return text
    out = []
    last = 0
    index = 0
    while index < len(positions):
        start = positions[index]
        end = start
        while (index + 1 < len(positions)
               and positions[index + 1] == end + 1):
            index += 1
            end = positions[index]
        if start < last:
            index += 1
            continue
        out.append(text[last:start])
        out.append(RESULT_COLOR)
        out.append(text[start:end + 1])
        out.append(RESET_COLOR)
        last = end + 1
        index += 1
    out.append(text[last:])
    return "".join(out)


def parse_choice(text, count):
    """Return ('pick', index) / ('quit', None) / ('refine', None) / ('invalid', None)."""
    cleaned = text.strip().lower()
    if cleaned in ("", "q", "quit", "exit"):
        return "quit", None
    if cleaned in ("r", "refine", "/"):
        return "refine", None
    if cleaned.isdigit():
        number = int(cleaned)
        if 1 <= number <= count:
            return "pick", number - 1
    return "invalid", None


def read_line(prompt):
    sys.stderr.write(prompt)
    sys.stderr.flush()
    line = sys.stdin.readline()
    if line == "":
        sys.stderr.write("\n")
        return None
    return line.rstrip("\r\n")


def choose(matches, total):
    """Show the numbered list and return a Match, REFINE, or None."""
    width = terminal_width()
    color = enable_ansi(sys.stderr)
    header = "  %d of %d match" % (len(matches), total)
    header += "es" if total != 1 else ""
    sys.stderr.write(header + "\n")
    for number, match in enumerate(matches, 1):
        shown, positions = fit_path(
            display_path(match.path), match.positions, width - 5)
        sys.stderr.write("%2d. %s\n" % (number, colorize(shown, positions, color)))
    sys.stderr.flush()
    while True:
        answer = read_line("select 1-%d [r=refine, q=quit]: " % len(matches))
        if answer is None:
            return None
        kind, index = parse_choice(answer, len(matches))
        if kind == "pick":
            return matches[index]
        if kind == "quit":
            return None
        if kind == "refine":
            return REFINE
        sys.stderr.write("invalid choice\n")


# --------------------------------------------------------------------------
# shell integration
# --------------------------------------------------------------------------

def launcher_argv():
    """The command line that starts this program, for embedding in wrappers."""
    override = os.environ.get("FOLDERFIND_LAUNCHER")
    if override:
        return [override]
    names = ["ff.exe", "ff.cmd", "ff.bat", "ff"] if IS_WINDOWS else ["ff"]
    for name in names:
        found = shutil.which(name)
        if not found:
            continue
        # Windows also allows .py in PATHEXT, and which() searches the current
        # directory first, so guard against picking up a bare script here.
        if os.path.basename(found).lower() == "ff.py":
            continue
        if (not IS_WINDOWS and os.path.basename(found) == "ff"
                and os.path.abspath(found) == os.path.abspath(__file__)):
            continue
        return [os.path.abspath(found)]
    return [sys.executable, os.path.abspath(__file__)]


def shell_snippet(kind):
    prefix = " ".join(shlex.quote(part) for part in launcher_argv())
    if kind in ("bash", "zsh", "sh"):
        return """ff() {
    local __ff_tmp __ff_dest __ff_rc
    __ff_tmp="$(mktemp "${TMPDIR:-/tmp}/ff.XXXXXX")" || return 1
    FOLDERFIND_OUT="$__ff_tmp" %s "$@"
    __ff_rc=$?
    if [ "$__ff_rc" -eq 0 ] && [ -s "$__ff_tmp" ]; then
        IFS= read -r __ff_dest < "$__ff_tmp" || true
        if [ -n "$__ff_dest" ]; then
            builtin cd -- "$__ff_dest" || __ff_rc=$?
        fi
    fi
    rm -f "$__ff_tmp"
    return "$__ff_rc"
}""" % prefix
    if kind == "fish":
        return """function ff --description 'fuzzy cd (folderfind)'
    set -l __ff_tmp (mktemp)
    set -lx FOLDERFIND_OUT $__ff_tmp
    %s $argv
    set -l __ff_rc $status
    if test $__ff_rc -eq 0; and test -s $__ff_tmp
        set -l __ff_dest (cat $__ff_tmp)
        if test -n "$__ff_dest"
            builtin cd -- $__ff_dest
            set __ff_rc $status
        end
    end
    rm -f $__ff_tmp
    return $__ff_rc
end""" % prefix
    if kind in ("powershell", "pwsh", "ps"):
        command = " ".join(ps_quote(part) for part in launcher_argv())
        return """function ff {
    $__ff_tmp = [System.IO.Path]::GetTempFileName()
    $__ff_prev = $env:FOLDERFIND_OUT
    try {
        $env:FOLDERFIND_OUT = $__ff_tmp
        & %s @args
        if ($LASTEXITCODE -eq 0) {
            $__ff_dest = [System.IO.File]::ReadAllText(
                $__ff_tmp, [System.Text.Encoding]::UTF8).Trim()
            if ($__ff_dest) { Set-Location -LiteralPath $__ff_dest }
        }
    } finally {
        if ($null -eq $__ff_prev) {
            Remove-Item Env:FOLDERFIND_OUT -ErrorAction SilentlyContinue
        } else {
            $env:FOLDERFIND_OUT = $__ff_prev
        }
        Remove-Item -LiteralPath $__ff_tmp -Force -ErrorAction SilentlyContinue
    }
}""" % command
    return """cmd.exe cannot change its own working directory from a child
process, so there is no wrapper for it.  Use ff to print the path, or
install the PowerShell integration instead:

    ff --init powershell"""


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def new_folder_note():
    """One-line reminder that the index has to be refreshed for new folders."""
    command = "ff -u"
    if enable_ansi(sys.stderr):
        command = ORANGE_COLOR + command + RESET_COLOR
        return (DIM_COLOR + "note: new folders won't appear until you re-index: "
                + RESET_COLOR + command)
    return "note: new folders won't appear until you re-index: " + command


def no_match_message(term, count):
    return ("no folder matched %r (the index has %s folders).\n"
            "try fewer or shorter words if you expected an older folder.\n%s"
            % (term, "{:,}".format(count), new_folder_note()))


def show_search_note():
    """Print the re-index hint after an indexed search (terminal only)."""
    if sys.stderr.isatty():
        print(new_folder_note(), file=sys.stderr)


def write_output(path):
    """Hand the chosen path to the wrapper (or stdout when run directly)."""
    out_file = os.environ.get("FOLDERFIND_OUT")
    if out_file:
        try:
            with open(out_file, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(path + "\n")
        except OSError as exc:
            print("error: could not write %s: %s" % (out_file, exc), file=sys.stderr)
            return 1
        return 0
    sys.stdout.write(path + "\n")
    sys.stdout.flush()
    if sys.stdout.isatty():
        print("tip: add the shell integration so ff changes directory directly "
              "(run: ff --init %s)" % shell_name(), file=sys.stderr)
    return 0


def shell_name():
    if IS_WINDOWS:
        return "powershell"
    name = os.path.basename(os.environ.get("SHELL", "bash")).lower()
    return name if name in ("bash", "zsh", "fish") else "bash"


def finish(match, args):
    if args.shell:
        shell = (os.environ.get("FOLDERFIND_SHELL")
                 or os.environ.get("SHELL")
                 or os.environ.get("COMSPEC")
                 or "/bin/sh")
        try:
            return subprocess.call([shell], cwd=match.path)
        except OSError as exc:
            print("error: could not start %s: %s" % (shell, exc), file=sys.stderr)
            return 1
    return write_output(match.path)


def cmd_stats(cfg):
    meta = load_meta()
    dirs = load_index()
    built = meta.get("built")
    print("ff %s (Python %s)" % (VERSION, sys.version.split()[0]))
    print("config : %s%s" % (
        cfg["config_path"],
        "" if os.path.exists(cfg["config_path"]) else "  (not created; using defaults)"))
    print("index  : %s" % index_path())
    if built:
        age = max(0.0, (time.time() - float(built)) / 86400.0)
        print("folders: %s" % "{:,}".format(len(dirs)))
        print("built  : %s (%.1f days ago)" % (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(float(built))), age))
        if meta.get("truncated"):
            print("note   : index hit max_entries and may be incomplete")
    else:
        print("folders: 0  (no index yet; run: ff -u)")
    print("roots  : %s" % ", ".join(cfg["roots"]))
    print("depth  : %d, max entries: %s" % (
        cfg["max_depth"], "{:,}".format(cfg["max_entries"])))
    return 0


def cmd_init(kind):
    kind = kind.lower()
    if kind == "ps":
        kind = "powershell"
    print(shell_snippet(kind))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ff",
        description="Fuzzy-find a folder, then cd there (via shell integration).",
        epilog="examples:  ff ai proj  |  ff -n 20 react  |  ff -u  |  ff --init powershell")
    parser.add_argument("terms", nargs="*", metavar="word",
                        help="words that should appear (fuzzily) in the folder path")
    pick = parser.add_argument_group("picking")
    pick.add_argument("-n", "--limit", type=int, default=None, metavar="N",
                      help="show at most N matches (default: 10, 0 = all)")
    pick.add_argument("-1", "--best", action="store_true",
                      help="take the best match without asking")
    pick.add_argument("-l", "--list", action="store_true",
                      help="print matching paths, one per line")
    pick.add_argument("-s", "--shell", action="store_true",
                      help="open a new shell in the chosen folder instead of printing it")
    index = parser.add_argument_group("index")
    index.add_argument("-i", "-u", "--update", "--index", dest="index",
                       action="store_true", help="rebuild the folder index")
    index.add_argument("--root", action="append", default=[], metavar="PATH",
                       help="extra root to index (repeatable; rebuilds the index)")
    index.add_argument("--depth", type=int, default=None, metavar="N",
                       help="max folder depth while indexing")
    setup = parser.add_argument_group("setup")
    setup.add_argument("--init", metavar="SHELL",
                       choices=["bash", "zsh", "fish", "powershell", "pwsh", "ps", "cmd"],
                       help="print shell integration for bash/zsh/fish/powershell")
    setup.add_argument("--stats", action="store_true",
                       help="show index location, size and roots")
    setup.add_argument("--config", metavar="FILE",
                       help="use an alternate config file")
    setup.add_argument("--version", action="version",
                       version="%(prog)s " + VERSION)
    return parser


def main(argv=None):
    setup_streams()
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)

    if args.init:
        return cmd_init(args.init)
    if args.stats:
        return cmd_stats(cfg)

    limit = cfg["results"] if args.limit is None else args.limit
    if limit <= 0:
        limit = None

    if args.index or args.root or args.depth is not None:
        rebuild_index(cfg, extra_roots=args.root, depth=args.depth)
        if not args.terms:
            return 0

    term = " ".join(args.terms).strip()
    if term in (".", "./", ".\\"):
        term = ""
    if term:
        candidate = os.path.abspath(os.path.expanduser(term))
        if os.path.isdir(candidate):
            if args.list:
                print(candidate)
                return 0
            return finish(Match(candidate), args)

    dirs = None
    while True:
        if term:
            if dirs is None:
                dirs, _meta = ensure_index(cfg)
                if not dirs:
                    return 1
            matches, total = search(dirs, term, limit, os.getcwd())
            if not matches:
                print(no_match_message(term, len(dirs)), file=sys.stderr)
                return 1
            show_search_note()
        else:
            matches, total = local_dirs(limit)
            if not matches:
                print("no folders inside %s" % os.getcwd(), file=sys.stderr)
                return 1

        if args.list:
            for match in matches:
                print(match.path)
            return 0
        if args.best or not sys.stdin.isatty():
            return finish(matches[0], args)

        selected = choose(matches, total)
        if selected is REFINE:
            answer = read_line("search: ")
            if answer is None or not answer.strip():
                return 1
            term = answer.strip()
            continue
        if selected is None:
            return 1
        return finish(selected, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # e.g. "ff --init bash | head" - the reader went away, stay quiet.
        try:
            sys.stdout.close()
        except OSError:
            pass
        sys.exit(0)
