"""Stop one externally verified orphaned Kaniko tree in its retained container.

Never use this inside a running Concourse build. See the accompanying runbook.
"""
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import sys
import time


def process(pid):
    raw = Path(f"/proc/{pid}/stat").read_text()
    end = raw.rfind(")")
    fields = raw[end + 2:].split()
    return {"pid": pid, "parent": int(fields[1]), "start": int(fields[19]), "state": fields[0]}


def snapshot():
    result = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                result[int(entry.name)] = process(int(entry.name))
            except FileNotFoundError:
                pass  # Exited between directory enumeration and stat.
    return result


def descendants(records, root):
    selected = {root}
    while True:
        added = {pid for pid, record in records.items() if record["parent"] in selected} - selected
        if not added:
            return selected
        selected.update(added)


def send(fd, sig):
    try:
        signal.pidfd_send_signal(fd, sig)
    except ProcessLookupError:
        pass  # The pinned process already exited; never signal a reused PID.


def pending(fds, seconds):
    deadline = time.monotonic() + seconds
    poller = select.poll()
    for fd in fds:
        poller.register(fd, select.POLLIN)
    alive = set(fds)
    while alive and time.monotonic() < deadline:
        for fd, _ in poller.poll(100):
            alive.discard(fd)
    return alive


def cleanup(pid, start, command_hash, cwd):
    if pid <= 1 or pid == os.getpid():
        raise RuntimeError("refusing init/self PID")
    if process(pid)["start"] != start:
        raise RuntimeError("root PID start time changed")
    command = Path(f"/proc/{pid}/cmdline").read_bytes()
    if command.split(b"\0")[0] != b"/kaniko/executor" or hashlib.sha256(command).hexdigest() != command_hash:
        raise RuntimeError("unexpected root command")
    if os.readlink(f"/proc/{pid}/cwd") != cwd:
        raise RuntimeError("unexpected working directory")
    held = {}  # PID -> (start time, pidfd), retained through verification.
    try:
        for _ in range(32):
            records = snapshot()
            if pid not in records or records[pid]["start"] != start:
                raise RuntimeError("root identity disappeared during freeze")
            if any(current in records and records[current]["start"] != identity[0]
                   for current, identity in held.items()):
                raise RuntimeError("frozen PID was reused")
            tree = descendants(records, pid)
            if os.getpid() in tree or len(tree) > 4096:
                raise RuntimeError("unsafe process tree")
            fresh = tree - held.keys()
            if not fresh:
                if all(records[current]["state"] in ("T", "t", "Z") for current in tree):
                    break
                time.sleep(0.1)  # Wait for SIGSTOP delivery before final tree scan.
                continue
            # Freeze the executor first so it cannot create another RUN stage.
            for current in sorted(fresh, key=lambda p: (p != pid, p)):
                expected = records[current]["start"]
                fd = os.pidfd_open(current)
                try:
                    if process(current)["start"] != expected:
                        raise RuntimeError("process identity changed before freeze")
                except BaseException:
                    os.close(fd)
                    raise
                held[current] = (expected, fd)
                send(fd, signal.SIGSTOP)
        else:
            raise RuntimeError("process tree did not stabilize")
        fds = [record[1] for record in reversed(list(held.values()))]
        for fd in fds:
            send(fd, signal.SIGTERM)
            send(fd, signal.SIGCONT)
        remaining = pending(fds, 10)
        for fd in remaining:
            send(fd, signal.SIGKILL)
        if pending(remaining, 10):
            raise RuntimeError("pinned processes remain alive")
        print(json.dumps({"stopped_processes": len(fds), "forced_processes": len(remaining)}))
    finally:
        for _, fd in held.values():
            try:
                send(fd, signal.SIGCONT)  # Thaw survivors if any preflight failed.
            finally:
                os.close(fd)


if __name__ == "__main__":
    if len(sys.argv) != 6 or sys.argv[1] != "--apply":
        raise SystemExit("usage: stop-orphaned-image-build.py --apply PID START CMDLINE_SHA256 CWD")
    cleanup(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5])
