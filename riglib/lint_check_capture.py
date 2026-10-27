"""Time- and memory-bounded capture for the external complexity analyzer."""
from __future__ import annotations

import queue
import subprocess
import threading
import time


class OutputLimitError(Exception):
    """Combined analyzer stdout/stderr exceeded the supported capture budget."""


def _send(messages, stopped, item) -> None:
    while not stopped.is_set():
        try:
            messages.put(item, timeout=0.05)
            return
        except queue.Full:
            continue


def _pump(pipe, label, messages, stopped) -> None:
    try:
        while not stopped.is_set():
            chunk = pipe.read1(65536)
            _send(messages, stopped, (label, chunk))
            if not chunk:
                return
    except OSError as exc:
        _send(messages, stopped, (label, exc))


def _collect(proc, messages, timeout: float, limit: int, started: float):
    buffers = [bytearray(), bytearray()]
    pending, captured = 2, 0
    while pending:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        try:
            label, chunk = messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise subprocess.TimeoutExpired(proc.args, timeout) from exc
        if isinstance(chunk, OSError):
            raise chunk
        if not chunk:
            pending -= 1
            continue
        captured += len(chunk)
        if captured > limit:
            raise OutputLimitError(f"analyzer output exceeds the {limit}-byte resource limit")
        buffers[label].extend(chunk)
    code = proc.wait(timeout=max(0, timeout - (time.monotonic() - started)))
    return subprocess.CompletedProcess(proc.args, code, bytes(buffers[0]), bytes(buffers[1]))


def capture(argv: list[str], *, cwd: str, timeout: float, limit: int):
    """Keep at most `limit` bytes plus four fixed-size pipe chunks, without temporary output files."""
    started = time.monotonic()
    messages = queue.Queue(maxsize=4)
    stopped = threading.Event()
    with subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        workers = [threading.Thread(target=_pump, args=(pipe, label, messages, stopped), daemon=True)
                   for label, pipe in enumerate((proc.stdout, proc.stderr))]
        for worker in workers:
            worker.start()
        try:
            return _collect(proc, messages, timeout, limit, started)
        finally:
            stopped.set()
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            for worker in workers:
                worker.join(timeout=1)
