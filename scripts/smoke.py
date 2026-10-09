"""Offline cross-process smoke run with structured phase evidence."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="eternalchat-smoke-") as data:
        base = [sys.executable, "-m", "eternalchat", "--data-dir", data, "--demo"]

        def run(phase, *args):
            result = subprocess.run(base + list(args), cwd=root, text=True,
                                    capture_output=True, timeout=30)
            if result.returncode:
                raise RuntimeError(f"{phase} failed: {result.stderr}")
            print(json.dumps({"phase": phase, "ok": True}), flush=True)
            return result.stdout

        run("first_turn", "ask", "The test project's code is amber-42.")
        recalled = run("restart_and_tool_recall", "ask", "/recall 0")
        assert "amber-42" in recalled, recalled
        raw = run("raw_retrieval", "zoom", "0")
        assert "amber-42" in raw, raw
        status = json.loads(run("inspect", "status"))
        assert status["messages"] == status["summarized_messages"], status
        assert status["failed"] == 0 and status["active"] == 0, status
        print(json.dumps({"phase": "complete", "ok": True, "status": status}), flush=True)


if __name__ == "__main__":
    main()
