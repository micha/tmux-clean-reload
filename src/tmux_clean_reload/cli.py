"""Reset the current tmux server's prefix bindings, then reload its config."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid


class ReloadError(Exception):
    """An actionable failure that should not produce a traceback."""


class Tmux:
    def __init__(self, executable, socket):
        self.executable = executable
        self.socket = socket

    def run(self, *arguments):
        return execute([self.executable, "-N", "-S", self.socket, *arguments])

    def identity(self):
        return self.run("display-message", "-p", "#{pid} #{start_time} #{version}").strip()

    def source(self, path, *, parse_only=False, pane=None):
        arguments = ["source-file"]
        if parse_only:
            arguments.append("-n")
        if pane:
            arguments.extend(["-t", pane])
        # source-file accepts glob patterns even when passed a single argv item.
        arguments.append(glob.escape(str(path)))
        return self.run(*arguments)


def execute(arguments):
    result = subprocess.run(arguments, capture_output=True, check=False)
    output = result.stdout.decode("utf-8", errors="surrogateescape")
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip() or output.strip()
        raise ReloadError(f"{arguments[0]}: {detail or f'exited with status {result.returncode}'}")
    if result.stderr:
        print(result.stderr.decode("utf-8", errors="replace").rstrip(), file=sys.stderr)
    return output


def current_tmux(executable):
    environment = os.environ.get("TMUX", "")
    parts = environment.rsplit(",", 2)
    if len(parts) != 3 or not parts[0] or not all(part.isdigit() for part in parts[1:]):
        raise ReloadError("run this command from a shell inside tmux (valid $TMUX required)")
    binary = shutil.which(executable)
    if not binary:
        raise ReloadError(f"tmux executable not found: {executable}; install tmux or use --tmux PATH")
    tmux = Tmux(binary, parts[0])
    identity = tmux.identity()
    identity_parts = identity.split()
    if len(identity_parts) != 3 or not all(part.isdigit() for part in identity_parts[:2]):
        raise ReloadError("could not determine the running tmux server's identity")
    server_version = identity_parts[-1]
    version_numbers = re.search(r"(\d+)\.(\d+)", server_version)
    if not version_numbers or tuple(map(int, version_numbers.groups())) < (3, 7):
        raise ReloadError("tmux 3.7+ is required for complete binding export (list-keys -F)")
    binary_version = execute([binary, "-V"]).strip().removeprefix("tmux ")
    if server_version != binary_version:
        raise ReloadError(
            f"server version {server_version} differs from executable version {binary_version}; "
            "use --tmux PATH with a matching executable"
        )
    return tmux, identity


def capture_bindings(tmux, *, start=False):
    # Random separators preserve arbitrary notes, including tabs and newlines.
    # Standard list-keys output omits notes, so it is not a complete backup.
    nonce = uuid.uuid4().hex
    field = f":{nonce}:field:"
    record = f":{nonce}:record:"
    names = ("key_table", "key_string", "key_repeat", "key_note", "key_command")
    format_string = field.join(f"#{{{name}}}" for name in names) + record
    arguments = ["list-keys", "-F", format_string]
    if start:
        output = execute([
            tmux.executable, "-S", tmux.socket, "-f", "/dev/null",
            "start-server", ";", *arguments,
        ])
    else:
        # List all tables: -T prefix fails when prefix is completely unbound.
        output = tmux.run(*arguments)
    bindings = []
    if not output:
        return bindings
    for row in output.split(record + "\n")[:-1]:
        fields = row.split(field)
        if len(fields) != len(names):
            raise ReloadError("could not decode tmux key bindings; live bindings were not reset")
        table, key, repeat, note, command = fields
        if table == "prefix":
            if not key or not command or repeat not in ("0", "1"):
                raise ReloadError("tmux lacks the required key binding formats")
            bindings.append({"key": key, "repeat": repeat == "1", "note": note, "command": command})
    if not output.endswith(record + "\n"):
        raise ReloadError("incomplete key binding export; live bindings were not reset")
    return bindings


def defaults(executable):
    with tempfile.TemporaryDirectory(prefix="tmux-clean-reload-") as directory:
        isolated = Tmux(executable, str(Path(directory) / "socket"))
        try:
            bindings = capture_bindings(isolated, start=True)
            if not bindings:
                raise ReloadError("temporary server returned no default prefix bindings")
            return bindings
        finally:
            # Only ever address our private socket; never the user's server.
            subprocess.run(
                [executable, "-N", "-S", isolated.socket, "kill-server"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )


def quote_tmux(value):
    """Quote one literal tmux argument, not a shell argument."""
    escapes = {
        "\\": "\\\\", '"': '\\"', "$": "\\$", "~": "\\~", "\n": "\\n", "\r": "\\r", "\t": "\\t",
    }
    return '"' + "".join(escapes.get(character, character) for character in value) + '"'


def binding_config(bindings):
    lines = ["unbind-key -aq -T prefix"]
    for binding in bindings:
        repeat = " -r" if binding["repeat"] else ""
        lines.append(
            f"bind-key{repeat} -T prefix -N {quote_tmux(binding['note'])} "
            f"{quote_tmux(binding['key'])} {binding['command']}"
        )
    return "\n".join(lines) + "\n"


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ReloadError(f"state directory must be owned by you with permissions 0700: {path}")


@contextmanager
def server_lock(socket):
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    if not base.is_absolute():
        raise ReloadError("XDG_STATE_HOME must be an absolute path")
    root = base / "tmux-clean-reload"
    private_directory(root)
    directory = root / hashlib.sha256(os.fsencode(socket)).hexdigest()[:16]
    private_directory(directory)
    with (directory / "lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReloadError("another clean reload or restore is already running for this server") from error
        yield directory


def save_snapshot(directory, tmux, identity, bindings):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    descriptor, filename = tempfile.mkstemp(prefix=f"snapshot-{timestamp}-", suffix=".json", dir=directory)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({
            "schema": 1, "socket": tmux.socket, "server": identity, "bindings": bindings,
        }, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return Path(filename)


def load_snapshot(path, tmux, identity):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise ReloadError(f"invalid snapshot: {path}: {error}") from error
    if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1:
        raise ReloadError(f"unsupported snapshot format: {path}")
    if data.get("socket") != tmux.socket or data.get("server") != identity:
        raise ReloadError("snapshot belongs to a different server or server lifetime")
    bindings = data.get("bindings")
    if not isinstance(bindings, list):
        raise ReloadError("snapshot bindings must be a list")
    keys = set()
    for binding in bindings:
        if (
            not isinstance(binding, dict)
            or set(binding) != {"key", "repeat", "note", "command"}
            or any(not isinstance(binding[name], str) for name in ("key", "note", "command"))
            or type(binding["repeat"]) is not bool
            or not binding["key"] or not binding["command"]
            or any("\0" in binding[name] for name in ("key", "note", "command"))
            or binding["key"] in keys
        ):
            raise ReloadError("snapshot contains an invalid or duplicate binding")
        keys.add(binding["key"])
    return bindings


def apply(tmux, bindings, directory):
    # Prepare and syntax-check the entire replacement before clearing the table.
    with tempfile.TemporaryDirectory(prefix="apply-", dir=directory) as temporary:
        path = Path(temporary) / "bindings.conf"
        path.write_bytes(binding_config(bindings).encode("utf-8", errors="surrogateescape"))
        tmux.source(path, parse_only=True)
        tmux.source(path)


def reload_or_restore(arguments):
    tmux, identity = current_tmux(arguments.tmux)
    pane = os.environ.get("TMUX_PANE")
    with server_lock(tmux.socket) as directory:
        if arguments.restore:
            replacement = load_snapshot(arguments.restore.expanduser().resolve(), tmux, identity)
            config = None
        else:
            config = arguments.config.expanduser().resolve(strict=True)
            if not config.is_file():
                raise ReloadError(f"config is not a regular file: {config}")
            tmux.source(config, parse_only=True, pane=pane)
            replacement = defaults(tmux.executable)

        before = capture_bindings(tmux)
        snapshot = save_snapshot(directory, tmux, identity, before)
        print(f"Snapshot: {snapshot}", flush=True)
        if tmux.identity() != identity:
            raise ReloadError("tmux server changed during preparation; refusing to reset bindings")
        try:
            apply(tmux, replacement, directory)
            if config:
                output = tmux.source(config, pane=pane)
                if output:
                    print(output, end="" if output.endswith("\n") else "\n")
        except (ReloadError, OSError, KeyboardInterrupt) as error:
            try:
                if tmux.identity() != identity:
                    raise ReloadError("tmux server changed; refusing to restore into a different server")
                apply(tmux, before, directory)
            except (ReloadError, OSError, KeyboardInterrupt) as rollback_error:
                raise ReloadError(
                    f"operation failed: {error}; binding rollback also failed: {rollback_error}\n"
                    f"Recovery snapshot: {snapshot}"
                ) from error
            raise ReloadError(
                f"operation failed: {error}; previous prefix bindings restored.\n"
                "Other config side effects are NOT rolled back.\n"
                f"Recovery: tmux-clean-reload --restore {shlex.quote(str(snapshot))}"
            ) from error
        print("Prefix bindings restored." if config is None else "Prefix bindings reset and config reloaded.")


def main():
    parser = argparse.ArgumentParser(
        description="Reset this tmux server's prefix bindings to defaults, then reload ~/.tmux.conf.",
        epilog=(
            "Requires tmux 3.7+. Only prefix bindings are reset. "
            "Options, other tables and config side effects are not rolled back."
        ),
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--config", type=Path, default=Path("~/.tmux.conf"), help="config to reload")
    operation.add_argument(
        "--restore", type=Path, metavar="SNAPSHOT", help="restore trusted prefix snapshot instead",
    )
    parser.add_argument(
        "--tmux", default="tmux", metavar="PATH", help="tmux executable matching the running server",
    )
    arguments = parser.parse_args()
    try:
        reload_or_restore(arguments)
    except (ReloadError, OSError) as error:
        print(f"tmux-clean-reload: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("tmux-clean-reload: interrupted before completion", file=sys.stderr)
        return 130
    return 0
