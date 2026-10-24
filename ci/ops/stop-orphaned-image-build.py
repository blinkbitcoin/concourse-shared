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

STOPPED = ("T", "t", "Z")


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


def ancestors(records, pid):
    selected = set()
    while pid in records and pid not in selected:
        selected.add(pid)
        pid = records[pid]["parent"]
    return selected


def active_unprotected(records, protected):
    return {
        pid: record for pid, record in records.items()
        if record["state"] != "Z" and (pid, record["start"]) not in protected
    }


def post_termination_targets(protected):
    return active_unprotected(snapshot(), protected)


def send(fd, sig):
    try:
        signal.pidfd_send_signal(fd, sig)
        return True
    except ProcessLookupError:
        return False  # The pinned process already exited; never signal a reused PID.


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
    held = {}  # (PID, start time) -> (pidfd, stopped by this helper).
    termination_started = False
    termination_uncertain = False
    forced = set()
    primary = None
    try:
        records = snapshot()
        if pid not in records or records[pid]["start"] != start:
            raise RuntimeError("root PID start time changed")
        tree = descendants(records, pid)
        if os.getpid() in tree:
            raise RuntimeError("unsafe process tree")
        helper_ancestors = ancestors(records, os.getpid())
        root_ancestors = ancestors(records, pid)
        shared_ancestors = (helper_ancestors & root_ancestors) - {1}
        if shared_ancestors:
            raise RuntimeError("target shares an untrusted supervisor with helper")
        # A wrapper above Kaniko belongs to the workload unless it is also in
        # the explicit PID 1 supervisor boundary.
        tree.update(root_ancestors - helper_ancestors)
        if len(tree) > 4096:
            raise RuntimeError("unsafe process tree")
        protected_pids = helper_ancestors - tree
        unrelated = {
            current for current, record in records.items()
            if record["state"] != "Z" and current not in tree and current not in protected_pids
        }
        if unrelated:
            raise RuntimeError("unrelated live process in retained container")
        protected = {(current, records[current]["start"]) for current in protected_pids}
        approved = {(current, records[current]["start"]) for current in tree}

        root = records[pid]
        fd = os.pidfd_open(pid)
        try:
            if process(pid)["start"] != start:
                raise RuntimeError("root identity changed before freeze")
        except BaseException:
            os.close(fd)
            raise
        held[(pid, start)] = (fd, root["state"] not in STOPPED)
        send(fd, signal.SIGSTOP)
        for _ in range(32):
            frozen = process(pid)
            if frozen["start"] != start:
                raise RuntimeError("root identity changed during freeze")
            if frozen["state"] in STOPPED:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("root did not stop")

        command = Path(f"/proc/{pid}/cmdline").read_bytes()
        if command.split(b"\0")[0] != b"/kaniko/executor" or hashlib.sha256(command).hexdigest() != command_hash:
            raise RuntimeError("unexpected root command")
        if os.readlink(f"/proc/{pid}/cwd") != cwd:
            raise RuntimeError("unexpected working directory")

        # Freeze every non-supervisory process in this dedicated terminal task
        # container. This keeps descendants in scope even if they reparent.
        for _ in range(32):
            records = snapshot()
            if pid not in records or records[pid]["start"] != start:
                raise RuntimeError("root identity disappeared during freeze")
            targets = active_unprotected(records, protected)
            if any((current, record["start"]) not in approved
                   for current, record in targets.items()):
                raise RuntimeError("new live process appeared during freeze")
            fresh = [
                (current, record) for current, record in targets.items()
                if (current, record["start"]) not in held
            ]
            if not fresh:
                if all(record["state"] in STOPPED for record in targets.values()):
                    break
                time.sleep(0.1)
                continue
            for current, record in sorted(fresh):
                identity = (current, record["start"])
                fd = os.pidfd_open(current)
                try:
                    if process(current)["start"] != record["start"]:
                        raise RuntimeError("process identity changed before freeze")
                except BaseException:
                    os.close(fd)
                    raise
                held[identity] = (fd, record["state"] not in STOPPED)
                send(fd, signal.SIGSTOP)
        else:
            raise RuntimeError("process tree did not stabilize")

        fds = [record[0] for record in reversed(list(held.values()))]
        for fd in fds:
            termination_uncertain = True
            try:
                delivered = send(fd, signal.SIGTERM)
            except OSError:
                termination_uncertain = False
                raise
            if delivered:
                termination_started = True
            termination_uncertain = False
            send(fd, signal.SIGCONT)
        remaining = pending(fds, 10)
        forced.update(remaining)
        for fd in remaining:
            send(fd, signal.SIGKILL)
        if pending(remaining, 10):
            raise RuntimeError("pinned processes remain alive")

        # A TERM handler may fork after the final freeze. A late process cannot
        # be safely distinguished from a concurrent administrative exec, so
        # detect it and fail closed rather than signaling it.
        escaped = post_termination_targets(protected)
        if escaped:
            raise RuntimeError("new live process appeared after termination")
        time.sleep(0.1)
        escaped = post_termination_targets(protected)
        if escaped:
            raise RuntimeError("new live process appeared after termination")

        print(json.dumps({"stopped_processes": len(held), "forced_processes": len(forced)}))
    except BaseException as exc:
        primary = exc
        raise
    finally:
        cleanup_errors = []
        for fd, stopped_here in held.values():
            if not termination_started and not termination_uncertain and stopped_here:
                try:
                    send(fd, signal.SIGCONT)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            try:
                os.close(fd)
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            if primary is not None:
                primary.add_note("cleanup errors: " + "; ".join(map(str, cleanup_errors)))
            else:
                raise cleanup_errors[0]


if __name__ == "__main__":
    if len(sys.argv) != 6 or sys.argv[1] != "--apply":
        raise SystemExit("usage: stop-orphaned-image-build.py --apply PID START CMDLINE_SHA256 CWD")
    cleanup(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5])
