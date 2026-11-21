# Agent guide

## Scope and sources of truth

This repository contains a standalone Python CLI, not a personal tmux config.
[README.md](README.md) owns the user-facing behavior, installation instructions,
scope and recovery limitations. This file owns agent maintenance guidance.
`pyproject.toml` owns Python package metadata and the console entry point;
`flake.nix` packages that same application for Nix.

Do not install or change a user's key bindings, edit their tmux config, run a
clean reload against their live server, or modify their real Nix/pipx profiles
without an explicit request. Use isolated profiles for packaging tests.

## Layout

- `src/tmux_clean_reload/cli.py`: command, tmux interaction, locking and recovery.
- `src/tmux_clean_reload/__main__.py`: `python -m tmux_clean_reload` entry point.
- `tests/test_cli.py`: standard-library unit tests and isolated tmux integration tests.
- `pyproject.toml`: pip/pipx packaging.
- `flake.nix` and `flake.lock`: reproducible Nix packaging and checks.

## Implementation conventions

- Support Python 3.10+ and tmux 3.7+ with standard-library Python runtime dependencies only.
- Use small functions, explicit error messages, four-space indentation and readable names.
- Keep line lengths near 100 characters; avoid unrelated formatting changes.
- Invoke subprocesses with argument arrays, never `shell=True`.
- Distinguish shell quoting, tmux configuration quoting and tmux argv semicolon escaping.
- Prefix defaults must come from the matching tmux executable on a private socket,
  never from the live server or a hardcoded copy of defaults.
- Reset only the prefix table. Expanding ownership requires an explicit design decision.
- Preserve binding commands, repeat flags and help notes in resets and snapshots.
- Prepare defaults and a durable recovery snapshot before mutating live bindings.
- Keep snapshot data private and reject cross-server or cross-lifetime restores.
- Serialize operations on a server; do not delete a lock file during normal cleanup.
- Treat configs and snapshots as trusted executable input, not sandboxed data.
- Do not claim atomicity or rollback of arbitrary config effects.
- Keep README behavior and CLI help aligned when making user-visible changes.

## Validation

Run from the repository root:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
nix flake check
nix build
```

Tests must use private tmux sockets, temporary `HOME` and `XDG_STATE_HOME`, and
synthetic configs. Never source `~/.tmux.conf` or call `kill-server` on a user's
socket. Track and clean up all test servers and child processes on failure too.
Missing-tmux integration skips are not a passing full validation run.

For packaging changes, exercise both `pipx install .` and `nix profile add .`
with temporary `PIPX_HOME`/`PIPX_BIN_DIR`/`PIPX_MAN_DIR` and a temporary Nix
`--profile`, respectively. Set `TCR_TEST_EXECUTABLE` to an installed command's
absolute path to run integration tests against that entry point instead of the
source module. Check the installed command's help and an isolated reload. Do not change the user's actual profiles.

Cover successful normalization, preserved out-of-scope state, repeated reloads,
empty prefix tables, notes/repeat flags/awkward quoting, calling-pane context,
config failures, snapshot recovery, version mismatches, concurrency and state
permission failures. Add focused regression tests for fixes. Report validation
commands and any skips, platform limitations or failures honestly.

For local Git flakes, add new files to Git's index before Nix validation; Nix
ignores untracked files. Do not commit, push or add a remote unless requested.
