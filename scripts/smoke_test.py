"""Start a freshly built ImageOptimizer executable and check /api/health.

Passes only if the app comes up and every encoder is found *inside the
bundle* (a path under PyInstaller's _MEI temp dir), so a tool that merely
happens to be on the build machine's PATH cannot hide a packaging mistake.

Usage: python scripts/smoke_test.py dist/ImageOptimizer[.exe] [--skip NAME ...]
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

TOOLS = ("pngquant", "oxipng", "cjpeg", "avifenc")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def stop(proc: subprocess.Popen):
    # A --onefile executable is a bootloader plus a child process: take down the whole tree.
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    proc.wait(timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("exe")
    parser.add_argument("--skip", nargs="*", default=[], choices=TOOLS,
                        help="Encoders that are expected to be absent")
    parser.add_argument("--timeout", type=int, default=90, help="Seconds to wait for startup")
    args = parser.parse_args()

    port = free_port()
    proc = subprocess.Popen(
        [args.exe, "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=(sys.platform != "win32"),
    )
    health = None
    try:
        deadline = time.time() + args.timeout
        while time.time() < deadline and health is None:
            if proc.poll() is not None:
                sys.exit(f"Executable exited early with code {proc.returncode}:\n{proc.stdout.read()}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as r:
                    health = json.load(r)
            except OSError:
                time.sleep(1)
        if health is None:
            sys.exit(f"No answer from /api/health within {args.timeout}s")
    finally:
        stop(proc)

    print(json.dumps(health, indent=2))
    problems = []
    for tool in TOOLS:
        if tool in args.skip:
            continue
        path = health.get(f"{tool}_path") or ""
        if not health.get(tool):
            problems.append(f"{tool}: not found")
        elif "_MEI" not in path:
            problems.append(f"{tool}: found at {path}, outside the bundle")
    if problems:
        sys.exit("Smoke test failed:\n  " + "\n  ".join(problems))
    print("Smoke test passed")


if __name__ == "__main__":
    main()
