# ff - fuzzy folder finder

`ff` finds directories whose **path** is fuzzy-close to the words you type,
shows the best matches as a numbered list, and changes into the one you pick.

```
PS D:\> ff model data

  3 of 3 matches
 1. D:\AI stuff\model training\datasets
 2. D:\AI stuff\model training\datasets\raw
 3. D:\backup\models\data
select 1-3 [r=refine, q=quit]: 1
PS D:\AI stuff\model training\datasets>
```

* Case-insensitive **subsequence** matching, so `ai fold` finds
  `D:\AI stuff\folderFind`. Multiple words all have to match and may match
  different parts of the path.
* Word starts, adjacent characters, and a matching folder name score higher;
  folders under your current directory get a small bonus.
* Searching a cached index makes a 200k-folder list feel instant
  (roughly 0.2-0.4 s per query).
* Zero dependencies. One Python file. Works on Windows, macOS and Linux.

## Install

### Windows (PowerShell)

```powershell
cd "D:\AI stuff\folderFind"
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer copies the tool to `%LOCALAPPDATA%\folderFind`, creates an
`ff.cmd` launcher, adds that folder to your **user PATH**, and appends the
`ff` function to your PowerShell profile. Open a new terminal afterwards.

Options: `-Dest <folder>`, `-NoProfile`, `-NoPath`.

### macOS / Linux

```bash
cd folderFind
./install.sh
```

Installs to `~/.local/bin`, adds it to `PATH` (in your shell rc file if
needed), and appends the `ff` function to `~/.bashrc`, `~/.zshrc` or
`~/.config/fish/config.fish`. Override the location with
`FOLDERFIND_BIN=/usr/local/bin ./install.sh`.

### pipx (any OS)

```bash
pipx install .
```

This puts `ff` on your PATH. `pipx` cannot edit your shell profile, so add the
function manually (see below) to get automatic `cd`. You can print the exact
line with:

```bash
ff --init powershell     # or bash / zsh / fish
```

## First run

```bash
ff -u        # build the folder index (once; takes a few seconds)
ff ai proj   # then just use it
```

The first search builds the index automatically if you skip `ff -u`.

## Why is there a shell function?

A program cannot change its parent shell's working directory. `ff` gets around
that the same way `zoxide`/`fzf` do: it prints the chosen path, and a tiny
shell function runs `cd` on it. That is what the installers set up:

```bash
ff() {
    local dest
    dest="$(command ff "$@")" || return $?
    [ -n "$dest" ] && builtin cd -- "$dest"
}
```

When `ff` detects the wrapper (via the `FOLDERFIND_OUT` environment variable)
it writes the path to a temp file instead of stdout, which also avoids any
encoding problems with non-ASCII folder names.

If you run `ff` **without** the wrapper it still works - it prints the path so
you can copy it - and tells you how to enable the integration.

`cmd.exe` cannot change its own directory from a child process at all, so
there is no `cmd` wrapper. Use PowerShell, or run `ff -s` to open a new shell
inside the chosen folder.

## Usage

```
ff [words ...] [options]

  ff                     pick from the folders in the current directory
  ff react               fuzzy-match "react"
  ff src test            both words must match (anywhere in the path)
  ff -n 25 data          show up to 25 matches
  ff -1 report           print the best match and exit (no prompt)
  ff -l report           print all matching paths, one per line
  ff -s                  open a new shell in the chosen folder
  ff -u                  rebuild the index
  ff -u --root D:\code   rebuild including an extra root
  ff -i --depth 4        rebuild with a shallower scan (faster)
  ff --stats             show index location, size, roots and age
  ff --init bash         print the shell function for bash/zsh/fish/powershell
```

At the prompt:

| input        | action                              |
| ------------ | ----------------------------------- |
| `1` .. `10`  | change into that folder             |
| `r`          | refine the search with new words    |
| `q` or Enter | cancel (no directory change)        |

When stdin is not a terminal (`ff foo | ...`, scripts) the best match is used
automatically, like `--best`.

## Configuration

`ff` reads `~/.folderfind.json` (optional). Every key can be omitted:

```json
{
  "roots": ["~", "D:\\code", "E:\\projects"],
  "exclude": ["my-huge-build-dir"],
  "max_depth": 10,
  "max_entries": 250000,
  "results": 10,
  "stale_days": 7
}
```

| key           | default                                | meaning |
| ------------- | -------------------------------------- | ------- |
| `roots`       | home + every fixed drive except `C:`   | where to index |
| `exclude`     | none (built-in list always applies)    | extra folder names to skip |
| `max_depth`   | `10`                                   | how deep below each root to scan |
| `max_entries` | `250000`                               | hard cap on indexed folders |
| `results`     | `10`                                   | default number of matches shown |
| `stale_days`  | `7`                                    | warn when the index is older than this |

The built-in skip list covers `.git`, `node_modules`, `__pycache__`, virtual
environments, `AppData`, `Temp`, `$Recycle.Bin`, `WinSxS` and similar noise.

Environment variables:

* `FOLDERFIND_CONFIG` - use a different config file.
* `FOLDERFIND_SHELL` - shell to start with `ff -s`.
* `FOLDERFIND_OUT` - internal, set by the wrapper functions.
* `NO_COLOR` - disable highlighted matches.

### After changing config

```bash
ff -u
```

Roots passed with `--root` are used for that rebuild only. Put permanent roots
in the config file.

## How matching works

Each search word must appear in the path as a subsequence (`fld` matches
`folder`). A match is scored by:

* word-boundary and start-of-string positions,
* consecutive characters,
* an exact match of the folder name,
* a small bonus for paths under the current directory,
* penalties for long paths, deep nesting and gaps.

The best matches are shown (10 by default; change it with `-n`). Typo
correction is not attempted - matching is fuzzy-by-subsequence.

## Limitations

* Symlinks and junctions are not indexed (they can create loops). They can
  still be reached through their real path.
* Directories you create after the last index run will not appear until
  `ff -u`.
* Network drives are not indexed unless you add them to `roots`.
* Folders you cannot read are skipped silently.

## Development

```bash
python -m unittest discover -s tests -v
```

`ff.py` is the whole program (about 900 lines); the tests cover scoring,
ranking, index walking, path display and the picker parser.

## Uninstall

```bash
# Windows
Remove-Item -Recurse "$env:LOCALAPPDATA\folderFind"
# then remove the folder from your user PATH and delete the
# "# >>> folderfind >>>" block from your PowerShell profile.

# macOS / Linux
rm -f ~/.local/bin/ff ~/.local/bin/ff.py
# then delete the "# >>> folderfind >>>" block from your shell rc file.
```

## License

MIT
