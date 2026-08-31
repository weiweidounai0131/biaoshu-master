#!/usr/bin/env python3
"""Local host-neutral service for bid production, review and delivery lock.

The browser persists local review/confirmation receipts only; it never calls
an AI provider. Any compatible host reads those receipts and generates or
re-exports the actual Word content outside the browser.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from . import protocol, export_image_plan, export_word
except ImportError:  # Direct CLI execution.
    import protocol
    import export_image_plan
    import export_word


HOST = "127.0.0.1"
DEFAULT_PORT = 5390
STATIC_DIR = Path(__file__).resolve().parent / "static"
LAUNCHED_PROCESSES: dict[tuple[str, int], subprocess.Popen[Any]] = {}


def canonical_project_dir(project_dir: Path) -> Path:
    """Use the physical path consistently in lock files and health checks."""
    return project_dir.expanduser().resolve()


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def lock_path(project_dir: Path) -> Path:
    return protocol.delivery_dir(project_dir) / protocol.LOCK_NAME


def load_lock(project_dir: Path) -> dict[str, Any] | None:
    path = lock_path(project_dir)
    if not path.exists():
        return None
    try:
        lock = protocol.read_json(path)
        pid = int(lock.get("pid", 0))
        port = int(lock.get("port", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return None
    if not process_alive(pid) or not 1 <= port <= 65535:
        path.unlink(missing_ok=True)
        return None
    return lock


def find_port(start: int) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((HOST, port))
            except OSError:
                continue
            return port
    raise RuntimeError("No free local delivery-review port found")


class DeliveryHandler(SimpleHTTPRequestHandler):
    server: "BidDeliveryServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[bid-delivery-ui] " + fmt % args + "\n")

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def send_json(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_payload(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("请求长度无效") from exc
        if length < 1 or length > 256_000:
            raise ValueError("请求内容长度无效")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求内容必须是JSON对象") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是JSON对象")
        return payload

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self.send_json({
                "ok": True,
                "service": "biaoshu-master-delivery-ui",
                "project": str(self.server.project_dir),
                "pid": os.getpid(),
                "port": self.server.server_port,
            })
            return
        if self.path == "/api/manifest":
            try:
                manifest = protocol.load_manifest(self.server.project_dir)
                self.send_json({"ok": True, "manifest": manifest, "manifest_sha256": protocol.sha256_data(manifest)})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path == "/api/session":
            try:
                manifest = protocol.load_manifest(self.server.project_dir)
                self.send_json({
                    "ok": True,
                    "status": manifest["status"],
                    "active_batch_id": manifest["active_batch_id"],
                    "word_batch_count": manifest["word_batch_count"],
                    "batch_statuses": [{"id": item["id"], "order": item["order"], "status": item["status"]} for item in manifest["word_batches"]],
                    "pending_request_count": manifest["pending_request_count"],
                    "last_event_id": manifest["last_event_id"],
                })
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path == "/api/workflow-link":
            try:
                data_dir = self.server.project_dir / protocol.confirm_ui.DATA_DIR_NAME
                confirmed = protocol.confirm_ui.stage4_confirmation_valid(data_dir)
                confirmation_url = None
                lock_path = self.server.project_dir / protocol.confirm_ui.LOCK_NAME
                if lock_path.exists():
                    lock = protocol.confirm_ui.read_json(lock_path)
                    pid = int(lock.get("pid", 0))
                    port = int(lock.get("port", 0))
                    if process_alive(pid) and 1 <= port <= 65535:
                        confirmation_url = f"http://{HOST}:{port}/final.html?view=stage4"
                if not confirmed:
                    self.send_json({"ok": True, "delivery_active": False, "confirmation_url": confirmation_url})
                    return
                manifest = protocol.load_manifest(self.server.project_dir)
                active = manifest.get("stage4_confirmation_sha256") == confirmed[2].get("confirmation_sha256")
                self.send_json({"ok": True, "delivery_active": active, "confirmation_url": confirmation_url})
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path == "/api/events":
            try:
                self.send_json({"ok": True, "events": protocol.list_events(self.server.project_dir)})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path == "/api/overview":
            try:
                self.send_json({"ok": True, "overview": protocol.delivery_overview_payload(self.server.project_dir)})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        reader_match = re.fullmatch(r"/api/batches/([^/]+)/reader(?:\?([^#]*))?", self.path)
        if reader_match:
            batch_id, query = reader_match.groups()
            options = urllib.parse.parse_qs(query or "", keep_blank_values=True)
            try:
                offset = int(options.get("offset", ["0"])[0])
                limit = int(options.get("limit", ["30"])[0])
                payload = protocol.batch_reader_payload(self.server.project_dir, batch_id, offset, limit)
                self.send_json({"ok": True, "reader": payload})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        validation_match = re.fullmatch(r"/api/batches/([^/]+)/validation", self.path)
        if validation_match:
            try:
                self.send_json({"ok": True, "word_validation": protocol.batch_validation_payload(self.server.project_dir, validation_match.group(1))})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        ai_recheck_match = re.fullmatch(r"/api/batches/([^/]+)/ai-recheck", self.path)
        if ai_recheck_match:
            try:
                self.send_json({"ok": True, "ai_recheck": protocol.ai_recheck_payload(self.server.project_dir, ai_recheck_match.group(1))})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path == "/api/image-plan":
            try:
                self.send_json({"ok": True, "image_plan": protocol.image_plan_payload(self.server.project_dir)})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path == "/api/final-delivery":
            try:
                self.send_json({"ok": True, "final_delivery": protocol.final_delivery_payload(self.server.project_dir)})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        requests_match = re.fullmatch(r"/api/batches/([^/]+)/requests", self.path)
        if requests_match:
            try:
                self.send_json({"ok": True, "requests": protocol.list_ai_requests(self.server.project_dir, requests_match.group(1))})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path in ("/", "/index.html"):
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        if self.path == "/api/shutdown":
            self.send_json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path == "/api/image-plan/direct-edits":
            try:
                payload = self.read_payload()
                result = protocol.apply_image_plan_direct_edit(
                    self.server.project_dir, payload.get("image_id"), payload.get("source_sha256"), payload.get("replacement"),
                )
                # Manual edits must never wait for a model.  Rebuild the
                # portable workbook locally and return it straight to review.
                exported = export_image_plan.export_image_plan(self.server.project_dir)
                self.send_json({"ok": True, "direct_edit": result, "export": exported})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path == "/api/image-plan/ai-requests":
            try:
                payload = self.read_payload()
                result = protocol.create_image_plan_ai_request(
                    self.server.project_dir, payload.get("image_id"), payload.get("source_sha256"), payload.get("instruction"),
                )
                self.send_json({"ok": True, "ai_request": result})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path == "/api/image-plan/confirm":
            try:
                _payload = self.read_payload()
                manifest, event = protocol.confirm_image_plan(self.server.project_dir)
                self.send_json({"ok": True, "manifest": manifest, "event": event})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        direct_match = re.fullmatch(r"/api/batches/([^/]+)/direct-edits", self.path)
        ai_match = re.fullmatch(r"/api/batches/([^/]+)/ai-requests", self.path)
        confirm_match = re.fullmatch(r"/api/batches/([^/]+)/confirm", self.path)
        wps_match = re.fullmatch(r"/api/batches/([^/]+)/wps-page-check", self.path)
        final_match = self.path == "/api/final-delivery/confirm"
        if not direct_match and not ai_match and not confirm_match and not wps_match and not final_match:
            self.send_json({"ok": False, "error": "不支持的本地审校操作"}, HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self.read_payload()
            batch_id = (direct_match or ai_match or confirm_match or wps_match).group(1) if not final_match else None
            if direct_match:
                result = protocol.apply_direct_edit(
                    self.server.project_dir, batch_id, payload.get("block_id"), payload.get("source_sha256"),
                    payload.get("replacement_text"), payload.get("replacement_items"),
                    payload.get("replacement_columns"), payload.get("replacement_rows"),
                )
                # This route is intentionally synchronous and deterministic:
                # the human's exact edit is persisted then re-exported without
                # awakening the current AI conversation.
                exported = export_word.export_word(self.server.project_dir, batch_id)
                self.send_json({"ok": True, "direct_edit": result, "export": exported})
            elif ai_match:
                result = protocol.create_ai_request(self.server.project_dir, batch_id, payload.get("block_id"), payload.get("source_sha256"), payload.get("instruction"))
                self.send_json({"ok": True, "ai_request": result})
            elif confirm_match:
                manifest, event = protocol.confirm_batch(self.server.project_dir, batch_id)
                self.send_json({"ok": True, "manifest": manifest, "event": event})
            elif wps_match:
                validation = protocol.record_wps_page_check(
                    self.server.project_dir, batch_id, payload.get("actual_pages"), payload.get("verifier", "用户WPS复核"),
                )
                self.send_json({"ok": True, "word_validation": validation})
            else:
                manifest, event = protocol.confirm_final_delivery(self.server.project_dir)
                self.send_json({"ok": True, "manifest": manifest, "event": event})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)


class BidDeliveryServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, project_dir: Path, port: int):
        self.project_dir = project_dir
        super().__init__((HOST, port), lambda *args, **kwargs: DeliveryHandler(*args, directory=str(STATIC_DIR), **kwargs))


def health(port: int, timeout: float = 1.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/health", timeout=timeout) as response:
            data = json.load(response)
        return data if isinstance(data, dict) else None
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def run_server(project_dir: Path, port: int) -> int:
    project_dir = canonical_project_dir(project_dir)
    protocol.load_manifest(project_dir)
    server = BidDeliveryServer(project_dir, port)
    protocol.atomic_write_json(lock_path(project_dir), {
        "schema_version": 1,
        "service": "biaoshu-master-delivery-ui",
        "pid": os.getpid(),
        "port": server.server_port,
        "project": str(project_dir),
        "started_at": protocol.utc_now(),
    })
    try:
        print(f"http://{HOST}:{server.server_port}", flush=True)
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        path = lock_path(project_dir)
        try:
            current = protocol.read_json(path)
            if int(current.get("pid", 0)) == os.getpid():
                path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    return 0


def launch_daemon(project_dir: Path, requested_port: int | None, no_browser: bool) -> int:
    project_dir = canonical_project_dir(project_dir)
    existing = load_lock(project_dir)
    if existing:
        url = f"http://{HOST}:{int(existing['port'])}"
        # Preserve the existing production/review tab when an agent reattaches.
        print(url)
        return 0
    port = requested_port if requested_port is not None else find_port(DEFAULT_PORT)
    # The daemon parent is the sole browser opener; the child must only serve.
    command = [sys.executable, str(Path(__file__).resolve()), str(project_dir), "--serve", "--port", str(port), "--no-browser"]
    log_path = protocol.delivery_dir(project_dir) / "service.log"
    log_handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    # Keep a local reference while this host is alive. This avoids treating a
    # deliberately detached local service as an unobserved child process.
    LAUNCHED_PROCESSES[(str(project_dir), process.pid)] = process
    deadline = time.time() + 10
    while time.time() < deadline:
        data = health(port)
        if data and data.get("service") == "biaoshu-master-delivery-ui" and data.get("project") == str(project_dir):
            # Stage 4 is already open in the confirmation tab.  That page
            # detects this ready service and replaces itself with the review
            # UI, so opening here would create a duplicate browser tab.
            print(f"http://{HOST}:{port}")
            return 0
        time.sleep(0.15)
    exit_detail = process.poll()
    if exit_detail is None:
        try:
            os.kill(process.pid, signal.SIGTERM)
        except OSError:
            pass
    tracked = LAUNCHED_PROCESSES.pop((str(project_dir), process.pid), None)
    if tracked is not None:
        try:
            tracked.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    print(f"Delivery-review service failed to start; see {log_path}", file=sys.stderr)
    return 3


def wait_for_event(project_dir: Path, event_type: str, timeout: int, after_event_id: int) -> int:
    event_path = protocol.wait_for_event(project_dir, event_type, timeout, after_event_id)
    if event_path is None:
        print(f"Timed out waiting for {event_type}; the delivery-review page remains available", file=sys.stderr)
        return 4
    print(str(event_path))
    return 0


def shutdown(project_dir: Path) -> int:
    project_dir = canonical_project_dir(project_dir)
    lock = load_lock(project_dir)
    if not lock:
        lock_path(project_dir).unlink(missing_ok=True)
        return 0
    port = int(lock["port"])
    request = urllib.request.Request(f"http://{HOST}:{port}/api/shutdown", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(request, timeout=2).read()
    except (OSError, urllib.error.URLError):
        try:
            os.kill(int(lock["pid"]), signal.SIGTERM)
        except OSError:
            pass
    for _ in range(30):
        if not process_alive(int(lock["pid"])):
            break
        time.sleep(0.1)
    tracked = LAUNCHED_PROCESSES.pop((str(project_dir), int(lock["pid"])), None)
    if tracked is not None:
        try:
            tracked.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    lock_path(project_dir).unlink(missing_ok=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--wait-only", action="store_true")
    parser.add_argument("--wait-event", choices=sorted(protocol.EVENT_TYPES | {"user-action"}), default="revision")
    parser.add_argument("--after-event-id", type=int, default=0)
    parser.add_argument("--wait-timeout", type=int, default=0, help="Seconds to wait; 0 waits indefinitely (default)")
    parser.add_argument("--shutdown", action="store_true")
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    project_dir = args.project_dir.expanduser().resolve()
    project_dir.mkdir(parents=True, exist_ok=True)
    actions = sum(bool(value) for value in (args.init, args.daemon, args.serve, args.wait_only, args.shutdown))
    if actions != 1:
        parser.error("choose exactly one of --init, --daemon, --wait-only, or --shutdown")
    if args.init:
        manifest, created = protocol.initialize_delivery(project_dir)
        print(json.dumps({
            "manifest": str(protocol.manifest_path(project_dir)),
            "created": created,
            "status": manifest["status"],
            "writing_rules": {
                "path": str(protocol.writing_rules_path(project_dir)),
                "sha256": manifest["writing_rules"]["project_sha256"],
                "instruction": "Stage4生成每个source/batch-NN.json前必须读取此规则文件，并把writing_rules_sha256写入源稿；规则快照缺失或摘要不一致时停止生产。",
            },
        }, ensure_ascii=False))
        return 0
    if args.shutdown:
        return shutdown(project_dir)
    if args.wait_only:
        return wait_for_event(project_dir, args.wait_event, args.wait_timeout, args.after_event_id)
    if args.daemon:
        protocol.load_manifest(project_dir)
        return launch_daemon(project_dir, args.port, args.no_browser)
    protocol.load_manifest(project_dir)
    port = args.port if args.port is not None else find_port(DEFAULT_PORT)
    return run_server(project_dir, port)


if __name__ == "__main__":
    raise SystemExit(main())
