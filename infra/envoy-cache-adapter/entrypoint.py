from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parent
CONFIG = Path("/tmp/turnstile-envoy/envoy.yaml")
JSON_SPACE = re.compile(r"[ \t\r\n]*")


def json_space_end(text: str, position: int) -> int:
    matched = JSON_SPACE.match(text, position)
    assert matched is not None
    return matched.end()


def rewrite_pool_deployment(body: bytes, deployment: str) -> bytes:
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", deployment):
        raise ValueError("Invalid pool deployment")
    text = body.decode("utf-8")
    decoder = json.JSONDecoder()
    position = json_space_end(text, 0)
    if text[position:position + 1] != "{":
        raise ValueError("Pool requests require a JSON object")
    position += 1
    span: tuple[int, int] | None = None
    while True:
        position = json_space_end(text, position)
        if text[position:position + 1] == "}":
            position += 1
            break
        key, position = decoder.raw_decode(text, position)
        position = json_space_end(text, position)
        if not isinstance(key, str) or text[position:position + 1] != ":":
            raise ValueError("Invalid pool request property")
        start = json_space_end(text, position + 1)
        value, position = decoder.raw_decode(text, start)
        if key == "model":
            if span is not None or not isinstance(value, str):
                raise ValueError("Pool requests require one model identity")
            span = (start, position)
        position = json_space_end(text, position)
        separator = text[position:position + 1]
        if separator == "}":
            position += 1
            break
        if separator != ",":
            raise ValueError("Invalid pool request separator")
        position += 1
        if text[json_space_end(text, position):].startswith("}"):
            raise ValueError("Invalid trailing pool request separator")
    if span is None or json_space_end(text, position) != len(text):
        raise ValueError("Pool request model identity is missing or invalid")
    return (text[:span[0]] + json.dumps(deployment) + text[span[1]:]).encode("utf-8")


class PoolDeploymentHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        try:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            replacement = rewrite_pool_deployment(
                body, self.headers.get("x-turnstile-pool-deployment", ""),
            )
        except (TypeError, ValueError):
            self.send_error(400, "Invalid pool deployment request")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(replacement)))
        self.end_headers()
        self.wfile.write(replacement)

    def log_message(self, format: str, *arguments: object) -> None:
        return


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"missing required setting: {name}")
    return value


def render_config() -> None:
    completion = os.environ.get("BUDGET_COMPLETION_ENABLED", "false").lower()
    if completion not in {"true", "false"}:
        raise RuntimeError("BUDGET_COMPLETION_ENABLED must be true or false")
    if completion == "true":
        from turnstile_core.integrations.budget_completion import BudgetCompletionClient

        BudgetCompletionClient(required("LEDGER_TABLE_ENDPOINT"), required("LEDGER_TABLE_NAME"),
                               lambda: "")
    adapter_key = required("ADAPTER_SHARED_KEY")
    if not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", adapter_key):
        raise RuntimeError("ADAPTER_SHARED_KEY has an invalid value")
    template = (ROOT / "envoy.yaml.template").read_text(encoding="utf-8")
    CONFIG.write_text(
        template.replace("__ADAPTER_SHARED_KEY__", adapter_key),
        encoding="utf-8",
    )


def main() -> None:
    render_config()
    rewriter = ThreadingHTTPServer(("127.0.0.1", 9081), PoolDeploymentHandler)
    rewriter.daemon_threads = True
    Thread(target=rewriter.serve_forever, daemon=True).start()
    shipper = subprocess.Popen(["python3", str(ROOT / "shipper.py")])
    envoy = subprocess.Popen(
        [
            "envoy",
            "-c",
            str(CONFIG),
            "--log-level",
            "info",
            "--file-flush-interval-msec",
            "100",
        ]
    )

    stopping = False

    def stop(_signal: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        envoy.terminate()
        shipper.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while envoy.poll() is None:
            if shipper.poll() is not None and not stopping:
                shipper = subprocess.Popen(["python3", str(ROOT / "shipper.py")])
            time.sleep(0.5)
    finally:
        rewriter.shutdown()
        rewriter.server_close()
        if shipper.poll() is None:
            shipper.terminate()
        shipper.wait(timeout=10)
    raise SystemExit(envoy.returncode or 0)


if __name__ == "__main__":
    main()