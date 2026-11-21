# tmux-clean-reload

Reset the current tmux server's **prefix key bindings** to its built-in defaults,
then reload your configuration. Run it from a shell inside tmux:

```sh
tmux-clean-reload
```

Ordinary tmux reloads execute configuration commands without undoing old ones.
This command removes stale prefix bindings and restores overridden defaults
before applying your current configuration. It saves a recovery snapshot first.

This repository owns the command, not your key bindings. Installation does not
modify any tmux configuration or register a shortcut.

## Install

Requires Python 3.10+ and tmux 3.7+ on a POSIX system (Linux or macOS).
The Python package has no third-party runtime dependencies. Complete binding
export uses `list-keys -F`, introduced in tmux 3.7. The tmux executable must match
the running server's version so that its defaults are appropriate for that server.

From this checkout, choose one:

### Nix

```sh
nix profile add .
```

The flake installs the Python console command and provides tmux as a fallback.
A tmux executable already on your `PATH` takes precedence. The flake exposes
packages for x86-64 Linux, ARM64 Linux and ARM64 macOS. Its pinned Nixpkgs no
longer supports Intel macOS; pipx remains an option there. Only platforms actually
exercised by your checks should be considered tested. Nix must have flakes enabled.

To run without installing:

```sh
nix run . -- --help
```

### pipx

Install [pipx](https://pipx.pypa.io/stable/installation/) and tmux separately, then:

```sh
pipx install .
```

Ensure pipx's binary directory is on your `PATH` (`pipx ensurepath` can help).
Both installation methods expose the same `tmux-clean-reload` command.

## Usage

```sh
# Reset prefix bindings, then source ~/.tmux.conf.
tmux-clean-reload

# Use an explicitly selected config, including XDG-based layouts.
tmux-clean-reload --config ~/.config/tmux/tmux.conf

# Select a tmux executable that matches your running server.
tmux-clean-reload --tmux /path/to/tmux

# Recover prefix bindings from a previously printed snapshot.
tmux-clean-reload --restore /absolute/path/to/snapshot.json
```

`--restore` and `--config` are mutually exclusive. Restore does not reload your
configuration. Each restore also saves the bindings it is about to replace.
Only restore snapshots you trust: they contain executable tmux binding commands.

### Server scope

The command selects the current server using `$TMUX` and does not start a new
live server if that server has stopped. Prefix bindings are server-wide, so one
invocation covers **all sessions on that server**. Other independently running
tmux servers need separate invocations from their own shells.

The configuration is sourced once, in the calling pane's context when
`$TMUX_PANE` is available. Global options apply normally to sessions/windows
that inherit them; session-local commands are not repeated for every session.

### What is reset

Only the `prefix` key table is reset. The command preserves binding commands,
repeat flags and help notes, including in recovery snapshots. Your configuration
can then deliberately disable defaults or replace them.

The reset itself does **not** alter:

- The `root`, copy-mode or custom key tables.
- Server, session, window or pane options.
- Hooks, user options, plugin state or workmux state.
- Sessions, windows, panes or the processes running inside them.

Sourcing your config can, of course, change any of those things. This is not a
complete factory reset, nor does it make appended options or arbitrary config
side effects idempotent. A config command can also alter a binding while this
script runs; avoid concurrent manual edits or reloads.

## Safety and recovery

The command:

1. Acquires a nonblocking, per-server lock in its state directory.
2. Checks the config's top-level syntax without executing it.
3. Computes defaults using a separate, config-free temporary tmux server.
4. Saves the current prefix bindings to a private snapshot and prints its path.
5. Syntax-checks and applies the replacement bindings, then sources the config.
6. Attempts to restore the previous prefix bindings if applying or reloading fails.

Snapshots live beneath:

```text
${XDG_STATE_HOME:-~/.local/state}/tmux-clean-reload/<socket-hash>/snapshot-*.json
```

The tool's state directories have mode `0700`; snapshots have mode `0600`.
Existing tool state directories must have private permissions and belong to you.
`XDG_STATE_HOME`, if set, must be absolute. Snapshot contents can include private
commands or notes; do not publish them casually.

Snapshots are retained until you delete them. They are restricted to the same
server socket and server lifetime, and are not portable backups for a new server.
Once satisfied with your reloads, you can remove old `snapshot-*.json` files;
leave the `lock` file alone while any invocation may be running.

**Reload is not transactional.** Rollback restores only prefix bindings. Options,
hooks, other key tables and any already executed config commands are not rolled
back. A syntax check does not execute nested `source-file` commands. Failures in
asynchronous jobs or deferred hooks cannot be reliably detected. Abrupt termination
such as `SIGKILL` can interrupt the operation without automatic rollback; use the
printed snapshot for manual recovery while the same server is still running.

On a detected error, the command exits nonzero and reports whether binding rollback
succeeded. It refuses version mismatches, overlapping invocations using the same
state directory, and snapshots from another server lifetime.

## Development

With Python and tmux on `PATH`:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Or use the supplied tools and package checks:

```sh
nix develop
nix flake check
nix build
```

Tests use temporary homes, state directories and private tmux sockets. They never
source your real config or modify your running tmux server. Integration tests skip
when tmux is unavailable; such a run is not complete validation. The Nix package
provides tmux and runs the suite during its build.

For agent-specific maintenance guidance, see [AGENTS.md](AGENTS.md).
The command semantics follow the [tmux manual](https://github.com/tmux/tmux/blob/master/tmux.1).
