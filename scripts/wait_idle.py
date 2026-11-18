#!/usr/bin/env python3
"""Wait until the NAS Byparr container is idle before a measured run.

Why this exists: Byparr solves one page at a time and the NAS instance is shared
with other clients (mdc/jackett). A run started while another client is solving
does not measure our providers — it measures the queue. This script is read-only:
it polls ``docker stats``, ``docker logs`` and ``/health`` and exits 0 only after
the container has been quiet for a sustained window.

Run it **on the NAS host** (it shells out to ``docker``)::

    python3 scripts/wait_idle.py --idle-seconds 30 --timeout 900

Idle means all three hold for ``--stable`` consecutive samples:

* no non-health request line in ``docker logs`` newer than ``--idle-seconds``;
* container CPU below ``--cpu-limit`` percent;
* ``/health`` answers in under ``--health-limit-ms``.

``GET /health`` lines are excluded from the activity test: this script's own
probes would otherwise reset the window forever.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

DEFAULT_CONTAINER = "byparr"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8191/health"

#: ``docker logs --timestamps`` prefixes each line with an RFC3339 UTC stamp.
_LOG_STAMP = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s")


def _run(command: list[str], *, timeout: float, combine: bool = False) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(command)} exited {result.returncode}: {result.stderr.strip()[:200]}"
        )
    # ``docker logs`` writes the container's stdout to our stdout and its stderr to
    # our stderr; a container that logs to stderr would otherwise look silent and
    # idle forever.
    if combine:
        return result.stdout + result.stderr
    return result.stdout


def cpu_percent(container: str) -> float:
    """Current CPU percentage from ``docker stats`` (one sample)."""

    raw = _run(
        ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", container],
        timeout=20,
    ).strip()
    return float(raw.rstrip("%") or 0.0)


def last_activity_age_seconds(container: str, *, tail: int = 60) -> float | None:
    """Seconds since the newest non-health log line, ``None`` when there is none.

    Byparr logs both request starts (``From: …``) and completions (``Done …``),
    so any recent line means the solver is working.
    """

    raw = _run(
        ["docker", "logs", "--timestamps", "--tail", str(tail), container],
        timeout=20,
        combine=True,
    )
    newest: datetime | None = None
    for line in raw.splitlines():
        if "GET /health" in line:
            continue
        match = _LOG_STAMP.match(line)
        if match is None:
            continue
        stamp = datetime.fromisoformat(match.group("stamp").replace("Z", "+00:00"))
        if newest is None or stamp > newest:
            newest = stamp
    if newest is None:
        return None
    return max(0.0, (datetime.now(UTC) - newest).total_seconds())


def health_latency_ms(url: str, *, timeout: float) -> tuple[bool, float, str]:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - LAN URL
            body = response.read(200).decode("utf-8", "replace")
        return True, (time.monotonic() - started) * 1000.0, body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, (time.monotonic() - started) * 1000.0, type(exc).__name__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    parser.add_argument("--idle-seconds", type=float, default=30.0)
    parser.add_argument("--cpu-limit", type=float, default=25.0)
    parser.add_argument("--health-limit-ms", type=float, default=3000.0)
    parser.add_argument("--stable", type=int, default=2, help="consecutive idle samples")
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--json", action="store_true", help="print one JSON line per sample")
    args = parser.parse_args(argv)

    deadline = time.monotonic() + args.timeout
    streak = 0
    sample = 0
    while time.monotonic() < deadline:
        sample += 1
        try:
            cpu = cpu_percent(args.container)
            age = last_activity_age_seconds(args.container)
            healthy, health_ms, detail = health_latency_ms(
                args.health_url, timeout=max(5.0, args.health_limit_ms / 1000.0 * 2)
            )
        except (RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
            print(f"[wait_idle] sample={sample} error={exc}", file=sys.stderr, flush=True)
            streak = 0
            time.sleep(args.poll)
            continue

        quiet = age is None or age >= args.idle_seconds
        idle = healthy and quiet and cpu <= args.cpu_limit and health_ms <= args.health_limit_ms
        streak = streak + 1 if idle else 0
        payload = {
            "sample": sample,
            "idle": idle,
            "streak": streak,
            "cpu_percent": round(cpu, 2),
            "last_activity_age_s": None if age is None else round(age, 1),
            "health_ok": healthy,
            "health_ms": round(health_ms, 1),
            "health": detail,
        }
        if args.json:
            print(json.dumps(payload), flush=True)
        else:
            age_text = "none" if age is None else f"{age:.0f}s"
            print(
                f"[wait_idle] sample={sample} idle={idle} cpu={cpu:.0f}% "
                f"quiet={age_text} health={health_ms:.0f}ms streak={streak}/{args.stable}",
                flush=True,
            )

        if streak >= args.stable:
            print("[wait_idle] Byparr is idle; safe to start.", flush=True)
            return 0
        time.sleep(args.poll)

    print(
        f"[wait_idle] timed out after {args.timeout:.0f}s; Byparr never went idle.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
