from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import traceback
import zipfile
from shutil import rmtree
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from paths import HAND_FONT, INDEX, LOGS
from signature import remove_signature, save_signature, signature_status, SIGNATURE

from main import (
    active_resources,
    add_resource,
    check_session,
    save_session,
    sign_in,
    cut_off_session,
    get_config,
    history,
    migrate_history,
    new_run_id,
    normalize_month,
    purge_files,
    public_message,
    purge_history,
    redact,
    remove_resource,
    RUN_ID,
    SHOTS,
    run_batch,
    run_month,
    save_config,
    save_signature_options,
    session_status,
    set_resource_excluded,
    storage_summary,
    type_sign_in,
    update_resource,
)
from workflow import DOWNLOADS, month_label

HOST = os.getenv("APP_HOST", "127.0.0.1")
PORT = int(os.getenv("APP_PORT", "8765"))

_lock = threading.Lock()
_running = False
_checking = False  # a session check or cut-off holds the Chrome profile, so runs wait for it
_session_task = ""  # "check" or "cutoff" while _checking
_current_run: dict | None = None


# --- Logs -----------------------------------------------------------------------
# Everything a run or session check prints (workflow steps, tracebacks) plus step/result lines.
# Each run keeps its own file, and a retry inside the run appends to it; the session check keeps
# only its latest attempt.

SESSION_LOG = LOGS / "session.jsonl"
_log_lock = threading.Lock()
_log_targets: dict[int, Path] = {}


def _run_log(run_id: str) -> Path | None:
    return LOGS / f"{run_id}.jsonl" if RUN_ID.fullmatch(run_id or "") else None


def _log_begin(path: Path | None, fresh: bool = False) -> None:
    """Send everything this thread logs or prints to `path` until `_log_end`."""
    if path is None:
        return
    LOGS.mkdir(exist_ok=True)
    with _log_lock:
        if fresh:
            path.write_text("", encoding="utf-8")
        _log_targets[threading.get_ident()] = path


def _log_end() -> None:
    with _log_lock:
        _log_targets.pop(threading.get_ident(), None)


def log(text: str, level: str = "info", image: str = "") -> None:
    """`image` is a screenshot file name under SHOTS/<run_id>, attached to the line."""
    at = datetime.now(timezone.utc).isoformat()
    lines = [redact(line.rstrip()) for line in text.splitlines() if line.strip()]
    records = [json.dumps({"at": at, "level": level, "text": line, **({"image": image} if image and i == len(lines) - 1 else {})}) + "\n"
               for i, line in enumerate(lines)]
    with _log_lock:
        path = _log_targets.get(threading.get_ident())
        if path is not None and records:
            with path.open("a", encoding="utf-8") as handle:
                handle.writelines(records)


def _logs(path: Path, after: int, active: bool) -> dict:
    """Lines numbered from 1; `epoch` changes when the file is started over (a new session check)."""
    with _log_lock:
        raw = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = []
    for seq, record in enumerate(raw, start=1):
        try:
            item = json.loads(record)
        except json.JSONDecodeError:
            continue
        if seq == 1 or seq > after:
            lines.append({"seq": seq, **item})
    epoch = lines[0]["at"] if lines and lines[0]["seq"] == 1 else ""
    return {"epoch": epoch, "active": active, "lines": [line for line in lines if line["seq"] > after]}


def _logged_runs() -> list[str]:
    return sorted(path.stem for path in LOGS.glob("*.jsonl") if RUN_ID.fullmatch(path.stem)) if LOGS.exists() else []


def _purge_run_logs() -> None:
    for run_id in _logged_runs():
        (LOGS / f"{run_id}.jsonl").unlink(missing_ok=True)
    rmtree(SHOTS, ignore_errors=True)


SHOT_NAME = re.compile(r"[A-Za-z0-9-]+\.png")


def _shot_path(run_id: str, name: str) -> Path | None:
    if not RUN_ID.fullmatch(run_id or "") or not SHOT_NAME.fullmatch(name or ""):
        return None
    path = SHOTS / run_id / name
    return path if path.is_file() else None


class _Tee(io.TextIOBase):
    """Keeps writing to the terminal and copies lines printed by logged threads into the live log."""

    def __init__(self, stream, level: str) -> None:
        self._stream, self._level, self._partial = stream, level, {}

    def write(self, text: str) -> int:
        self._stream.write(text)
        ident = threading.get_ident()
        if ident in _log_targets:
            *lines, self._partial[ident] = (self._partial.get(ident, "") + text).split("\n")
            for line in lines:
                log(line, self._level)
        return len(text)

    def flush(self) -> None:
        self._stream.flush()


def _state() -> dict:
    with _lock:
        running = _running
        current_run = dict(_current_run) if _current_run else None
    entries = []
    for entry in history():
        item = dict(entry)
        item["files"] = _file_details(item.get("files", []))
        entries.append(item)
    config = get_config()
    return {
        "running": running,
        "logged_runs": _logged_runs(),
        "session": {**session_status(), "checking": _is_checking(), "task": _session_task, "has_logs": SESSION_LOG.exists()},
        "current_run": current_run,
        "history": entries,
        "resources": config["resources"],
        "month_value": config["month_value"],
        "signin_url": _signin_url(),
    }


def _file_details(paths: list[str]) -> list[dict]:
    details = []
    for raw in paths:
        target = _download_target(raw)
        if target is None or target.suffix.casefold() != ".pdf":
            continue
        details.append({"path": raw, "size": target.stat().st_size})
    return details


def _start_batch(month_value: str, only: list[str] | None = None, retry_of: str | None = None) -> bool:
    """Start a run for every active resource, or only for `only`.

    With `retry_of`, `only` is re-run inside that existing run: same run ID and month, results replace the failed entries.
    """
    global _current_run, _running
    config = get_config()
    if retry_of:
        month_value = run_month(retry_of) or ""
        if not month_value:
            raise ValueError("That run no longer exists")
        if not only:
            raise ValueError("Pick the resources to retry")
    if only:
        known = {item["name"].casefold(): item["name"] for item in config["resources"]}
        missing = [name for name in only if name.casefold() not in known]
        if missing:
            raise ValueError(f"Not in the resource list anymore: {', '.join(missing)}")
        resources = list(dict.fromkeys(known[name.casefold()] for name in only))
    else:
        resources = active_resources(config)
    if not resources:
        return False
    run_id = retry_of or new_run_id()
    with _lock:
        if _checking:
            raise ValueError("A session check is in progress — try again in a moment")
        if _running:
            return False
        _running = True
        _current_run = {
            "run_id": run_id,
            "retry": bool(retry_of),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "resources": resources,
            "resource_name": resources[0],
            "month_value": month_value,
            "index": 1,
            "total": len(resources),
            "completed": 0,
            "succeeded": 0,
        }
    if not retry_of:
        save_config(config["resources"], month_value)

    def progress(resource_name: str, index: int, total: int) -> None:
        with _lock:
            if _current_run is not None:
                _current_run.update(resource_name=resource_name, index=index, total=total)
        if total > 1:
            log(f"{resource_name} ({index} of {total})", "step")

    def result(entry: dict) -> None:
        if entry["status"] == "ok":
            count = len(entry["files"])
            log(f"{entry['resource_name']} is done · {count} PDF{'s' if count != 1 else ''} saved", "ok")
        else:
            log(f"Could not export {entry['resource_name']} · {(entry['message'] or 'Unknown error').splitlines()[0]}", "error",
                image=entry.get("screenshot", ""))

    def worker() -> None:
        global _current_run, _running
        _log_begin(_run_log(run_id), fresh=not retry_of)
        who = ", ".join(resources) if len(resources) <= 3 else f"{len(resources)} resources"
        log(f"{'Retrying' if retry_of else 'Exporting'} {who} for {month_label(month_value)}", "step")
        try:
            results = run_batch(resources, month_value, on_progress=progress, run_id=run_id, on_result=result)
            succeeded = sum(1 for item in results if item["status"] == "ok")
            failed = len(results) - succeeded
            if succeeded:
                save_session("healthy", "Power BI opens without asking to sign in.")
            if not failed:
                log(f"All done · {succeeded} exported", "ok")
            elif succeeded:
                log(f"Finished with problems · {succeeded} exported, {failed} failed", "error")
            else:
                log(f"Nothing was exported · {failed} failed", "error")
            with _lock:
                if _current_run is not None:
                    _current_run.update(completed=len(results), succeeded=succeeded)
        except Exception as exc:
            log(f"The run stopped unexpectedly · {public_message(exc)}", "error")
            raise
        finally:
            _log_end()
            with _lock:
                _running = False
                _current_run = None

    threading.Thread(target=worker, name="export-run", daemon=True).start()
    return True


def _download_target(raw: str) -> Path | None:
    if not raw or raw.startswith("/") or ".." in Path(raw).parts:
        return None
    root = DOWNLOADS.resolve()
    candidate = (root / raw).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _zip(targets: list[Path]) -> bytes:
    """Zip files as `Resource/file.pdf` so each person's PDFs stay together."""
    root = DOWNLOADS.resolve()
    buffer = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for target in dict.fromkeys(targets):
            relative = target.relative_to(root)
            name = f"{relative.parts[0]}/{target.name}"
            if name in used:
                name = relative.as_posix()
            used.add(name)
            archive.write(target, name)
    return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/state":
            self._send(200, "application/json", json.dumps(_state()).encode())
            return
        if parsed.path == "/fonts/IndieFlower-Regular.ttf":
            if not HAND_FONT.is_file():
                self._send(404, "text/plain; charset=utf-8", b"Font not found")
                return
            self._send(200, "font/ttf", HAND_FONT.read_bytes())
            return
        if parsed.path == "/signature.png":
            if not signature_status()["saved"]:
                self._send(404, "text/plain; charset=utf-8", b"No signature")
                return
            self._send(200, "image/png", SIGNATURE.read_bytes())
            return
        if parsed.path == "/logs/shot":
            query = parse_qs(parsed.query)
            path = _shot_path(query.get("run", [""])[0], query.get("name", [""])[0])
            if path is None:
                self._send(404, "text/plain; charset=utf-8", b"Screenshot not found")
                return
            self._send(200, "image/png", path.read_bytes())
            return
        if parsed.path == "/api/logs":
            query = parse_qs(parsed.query)
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                after = 0
            if "session" in query:
                body = _logs(SESSION_LOG, after, _is_checking())
            else:
                run_id = query.get("run", [""])[0]
                path = _run_log(run_id)
                if path is None:
                    self._send(400, "application/json", b'{"error": "Unknown run ID"}')
                    return
                with _lock:
                    active = _running and bool(_current_run) and _current_run.get("run_id") == run_id
                body = _logs(path, after, active)
            self._send(200, "application/json", json.dumps(body).encode())
            return
        if parsed.path == "/api/settings":
            self._send(200, "application/json", json.dumps(_settings()).encode())
            return
        if parsed.path == "/download":
            target = _download_target(parse_qs(parsed.query).get("path", [""])[0])
            if target is None:
                self._send(404, "text/plain; charset=utf-8", b"File not found")
                return
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/download/zip":
            query = parse_qs(parsed.query)
            targets = [target for target in map(_download_target, query.get("path", [])) if target is not None]
            if not targets:
                self._send(404, "text/plain; charset=utf-8", b"No files to download")
                return
            name = re.sub(r"[^\w.-]+", "_", query.get("name", ["CRA"])[0]).strip("._") or "CRA"
            data = _zip(targets)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{name}.zip"')
            self.end_headers()
            self.wfile.write(data)
            return
        self._send(404, "text/plain; charset=utf-8", b"Not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/signature":
            self._upload_signature()
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/run":
                if _is_running():
                    self._send(409, "application/json", b'{"running": true}')
                    return
                session = session_status()
                if session.get("status") == "signed_out" or not session.get("has_profile"):
                    self._send(409, "application/json", json.dumps({"error": "The Power BI session is logged out. Sign in first."}).encode())
                    return
                month_value = normalize_month(str(payload.get("month_value", "")))
                only = payload.get("resources")
                if only is not None and (not isinstance(only, list) or not all(isinstance(n, str) for n in only)):
                    raise ValueError("resources must be a list of names")
                retry_of = payload.get("retry_of")
                if retry_of is not None and not isinstance(retry_of, str):
                    raise ValueError("retry_of must be a run ID")
                if not _start_batch(month_value, only, retry_of):
                    self._send(400, "application/json", json.dumps({"error": "No active resources — add one or un-exclude one first"}).encode())
                    return
                self._send(202, "application/json", b'{"running": true}')
                return
            if path == "/api/session/type":
                type_sign_in(str(payload.get("text", "")))
                self._send(200, "application/json", b"{}")
                return
            if path in ("/api/session/check", "/api/session/signin", "/api/session/cutoff", "/api/purge/files", "/api/purge/history", "/api/signature/remove"):
                status, body = _settings_action(path, payload)
                self._send(status, "application/json", json.dumps(body).encode())
                return
            if path == "/api/signature/options":
                save_signature_options(payload)
                self._send(200, "application/json", json.dumps(_settings()).encode())
                return
            if path == "/api/resources":
                config = add_resource(str(payload.get("name", "")))
            elif path == "/api/resources/update":
                config = update_resource(str(payload.get("old_name", "")), str(payload.get("name", "")))
            elif path == "/api/resources/exclude":
                config = set_resource_excluded(str(payload.get("name", "")), bool(payload.get("excluded", False)))
            elif path == "/api/resources/delete":
                config = remove_resource(str(payload.get("name", "")))
            else:
                self._send(404, "text/plain; charset=utf-8", b"Not found")
                return
            self._send(200, "application/json", json.dumps(config).encode())
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send(400, "application/json", json.dumps({"error": str(exc)}).encode())

    def _upload_signature(self) -> None:
        if _is_running() or _is_checking():
            self._send(409, "application/json", json.dumps({"error": "Wait for the current run or session check to finish"}).encode())
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 2 * 1024 * 1024:
            self._send(400, "application/json", json.dumps({"error": "Choose a PNG image up to 2 MB"}).encode())
            return
        try:
            save_signature(self.rfile.read(length))
        except ValueError as exc:
            self._send(400, "application/json", json.dumps({"error": str(exc)}).encode())
            return
        self._send(200, "application/json", json.dumps(_settings()).encode())

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}")


def _is_running() -> bool:
    with _lock:
        return _running


def _is_checking() -> bool:
    with _lock:
        return _checking


def _settings() -> dict:
    entries = history()
    return {
        "running": _is_running(),
        "session": {**session_status(), "checking": _is_checking(), "task": _session_task},
        "storage": storage_summary(),
        "signature": {**signature_status(), **get_config()["signature"]},
        "history": {"runs": len({entry.get("run_id") for entry in entries}), "entries": len(entries)},
        "logs": {"runs": len(_logged_runs()), "bytes": sum(path.stat().st_size for path in LOGS.glob("*.jsonl")) if LOGS.exists() else 0},
        "signin_url": _signin_url(),
    }


def _signin_url() -> str:
    """Public address of the sign-in screen. Empty when Chrome opens on this machine instead."""
    return os.getenv("SIGNIN_URL", "").strip()


def _finish_sign_in() -> None:
    """Run the visible sign-in off the request so the browser tab is not tied to one long HTTP call."""
    global _checking, _session_task
    _log_begin(SESSION_LOG, fresh=True)
    log("Signing in to Power BI", "step")
    try:
        outcome = sign_in()
        if outcome["status"] == "healthy":
            log("Online · exports can run without you", "ok")
        else:
            log(f"Still logged out · {outcome['detail']}", "error")
    except Exception as exc:
        traceback.print_exc()
        log(f"Could not sign in · {public_message(exc)}", "error")
    finally:
        _log_end()
        with _lock:
            _checking = False
            _session_task = ""


def _settings_action(path: str, payload: dict) -> tuple[int, dict]:
    """Session check, session cut-off and purges; all of them need the exporter to be idle."""
    global _checking, _session_task
    method = str(payload.get("method", ""))
    if path == "/api/session/cutoff":
        if method not in ("sign_out", "forget"):
            return 400, {"error": "Pick how to cut off the session"}
        session = session_status()
        if not session["has_profile"]:
            return 409, {"error": "There is no saved sign-in on the server"}
        if method == "sign_out" and session["status"] == "signed_out":
            return 409, {"error": "Power BI is already logged out"}
    with _lock:
        if _running or _checking:
            return 409, {"error": "Wait for the current run or session check to finish"}
        if path in ("/api/session/check", "/api/session/signin", "/api/session/cutoff"):
            _checking = True
            _session_task = path.rsplit("/", 1)[-1]
    if path == "/api/session/signin":
        threading.Thread(target=_finish_sign_in, name="sign-in", daemon=True).start()
        return 200, _settings()
    elif path == "/api/session/cutoff":
        _log_begin(SESSION_LOG, fresh=True)
        log("Logging out of Power BI" if method == "sign_out" else "Deleting the saved sign-in", "step")
        try:
            outcome = cut_off_session(method)
            log(f"Session cut off · {outcome['detail']}", "ok")
        except Exception as exc:
            traceback.print_exc()
            message = public_message(exc)
            log(f"Could not cut off the session · {message}", "error")
            return 500, {"error": message}
        finally:
            _log_end()
            with _lock:
                _checking = False
                _session_task = ""
    elif path == "/api/session/check":
        _log_begin(SESSION_LOG, fresh=True)
        log("Checking your Power BI sign-in", "step")
        try:
            outcome = check_session()
            if outcome["status"] == "healthy":
                log("Online · exports can run without you", "ok")
            elif outcome["status"] == "signed_out":
                log("Logged out · sign in before running exports", "error")
            else:
                log(f"Could not confirm your sign-in · {outcome['detail']}", "error")
        finally:
            _log_end()
            with _lock:
                _checking = False
                _session_task = ""
    elif path == "/api/purge/files":
        purge_files()
    elif path == "/api/signature/remove":
        remove_signature()
    else:
        purge_history()
        _purge_run_logs()
    return 200, _settings()


def main() -> None:
    # Only stdout reaches the app's logs; tracebacks and driver noise on stderr stay in the terminal.
    sys.stdout = _Tee(sys.stdout, "info")
    migrate_history()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Open http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
