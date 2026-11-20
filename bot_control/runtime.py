import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import psutil


@contextmanager
def operation_lock(path):
    with Path(path).open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BlockingIOError("Control panel operation is already running") from None
        yield


def fingerprint(args):
    return hashlib.sha256(json.dumps(args, ensure_ascii=True).encode()).hexdigest()


class Processes:
    def record(self, pid):
        process = psutil.Process(pid)
        return {
            "pid": pid,
            "created": process.create_time(),
            "exe": process.exe(),
            "command_hash": fingerprint(process.cmdline()),
        }

    def alive(self, record):
        try:
            current = self.record(record["pid"])
            return (
                abs(current["created"] - record["created"]) < 0.1
                and current["exe"].casefold() == record["exe"].casefold()
                and current["command_hash"] == record["command_hash"]
            )
        except (psutil.Error, KeyError, TypeError):
            return False

    def listener(self, port):
        found = {
            item.pid
            for item in psutil.net_connections(kind="tcp")
            if item.status == psutil.CONN_LISTEN and item.laddr.port == port and item.pid
        }
        return next(iter(found)) if len(found) == 1 else None

    def matches(self, pid, *, cwd, module=None, script=None, executable=None):
        try:
            process = psutil.Process(pid)
            if Path(process.cwd()).resolve() != Path(cwd).resolve():
                return False
            if executable and Path(process.exe()).resolve() != Path(executable).resolve():
                return False
            args = process.cmdline()
            if module:
                return "-m" in args and args[args.index("-m") + 1] == module
            if script:
                return any(
                    (Path(cwd) / arg).resolve() == Path(script).resolve()
                    for arg in args[1:]
                    if not arg.startswith("-")
                )
            return executable is not None
        except (psutil.Error, OSError, IndexError):
            return False

    def spawn(self, args, cwd, environment, log):
        flags = (
            (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
            if os.name == "nt"
            else 0
        )
        with Path(log).open("ab", buffering=0) as output:
            process = subprocess.Popen(
                args,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                creationflags=flags,
            )
        return self.record(process.pid)

    def stop(self, record):
        if not self.alive(record):
            return False
        process = psutil.Process(record["pid"])
        children = process.children(recursive=True)
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
        psutil.wait_procs(children + [process], timeout=5)
        return True
