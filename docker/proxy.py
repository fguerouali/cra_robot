"""One public port: the app, plus the sign-in screen under /vnc/."""

from __future__ import annotations

import os
import select
import socket
import threading

APP = ("127.0.0.1", 8765)
VNC = ("127.0.0.1", 6080)


def _destination(path: bytes) -> tuple[tuple[str, int], bytes]:
    raw, sep, query = path.partition(b"?")
    tail = (sep + query) if sep else b""
    if raw == b"/websockify" or raw.startswith(b"/websockify/"):
        return VNC, path
    if raw == b"/vnc/vnc_embed.html":
        return VNC, b"/vnc.html" + tail
    if raw == b"/vnc" or raw.startswith(b"/vnc/"):
        stripped = raw[len(b"/vnc") :] or b"/"
        return VNC, stripped + tail
    return APP, path


def _upgrade(header: bytes) -> bool:
    for line in header.split(b"\r\n")[1:]:
        if line.lower().startswith(b"upgrade:") and b"websocket" in line.lower():
            return True
    return False


def _content_length(header: bytes) -> int:
    for line in header.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            try:
                return int(line.split(b":", 1)[1].strip())
            except ValueError:
                return 0
    return 0


def _rewrite(header: bytes) -> tuple[tuple[str, int], bytes, bool]:
    lines = header.split(b"\r\n")
    method, path, version = lines[0].split(b" ", 2)
    target, new_path = _destination(path)
    lines[0] = b" ".join((method, new_path, version))
    upgrade = _upgrade(header)
    rewritten = [lines[0]]
    saw_connection = False
    for line in lines[1:]:
        if line.lower().startswith(b"connection:"):
            saw_connection = True
            rewritten.append(line if upgrade else b"Connection: close")
            continue
        rewritten.append(line)
    if not upgrade and not saw_connection:
        rewritten.append(b"Connection: close")
    return target, b"\r\n".join(rewritten) + b"\r\n\r\n", upgrade


def _read_headers(conn: socket.socket) -> tuple[bytes, bytes] | None:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return None
        data += chunk
        if len(data) > 1024 * 1024:
            return None
    header, rest = data.split(b"\r\n\r\n", 1)
    return header, rest


def _tunnel(left: socket.socket, right: socket.socket) -> None:
    pair = [left, right]
    try:
        while True:
            ready, _, _ = select.select(pair, [], [], 3600)
            if not ready:
                return
            for sock in ready:
                other = right if sock is left else left
                chunk = sock.recv(65536)
                if not chunk:
                    return
                other.sendall(chunk)
    finally:
        for sock in pair:
            try:
                sock.close()
            except OSError:
                pass


def _nodelay(sock: socket.socket) -> None:
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


def _vnc_shell(header: bytes) -> bytes:
    """The picture is Power BI. Clicks and keys on it are forwarded into Chrome, so Authenticator can be used."""
    path = header.split(b"\r\n", 1)[0].split(b" ")[1]
    query = path.split(b"?", 1)[1].decode("ascii", "ignore") if b"?" in path else ""
    src = "/vnc/vnc_embed.html" + (("?" + query) if query else "")
    src = src.replace("&", "&amp;").replace('"', "&quot;")
    page = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connexion Power BI</title>
<style>
  html, body { margin: 0; height: 100%; background: #111; color: #111; }
  #hint { position: fixed; top: 0; left: 0; right: 0; height: 36px; z-index: 4; margin: 0;
    display: flex; align-items: center; padding: 0 12px; box-sizing: border-box;
    background: #f5c518; font: 600 14px/1.2 sans-serif; }
  iframe, #pad { position: fixed; top: 36px; left: 0; width: 100%; height: calc(100% - 36px); border: 0; background: #000; }
  iframe { pointer-events: none; }
  #pad { z-index: 3; background: transparent; outline: none; cursor: default; }
</style>
</head>
<body>
<p id="hint">Clique dans l'image et tape au clavier. L'Authenticator se fait ici.</p>
<iframe src="__SRC__" title="Power BI"></iframe>
<div id="pad" tabindex="0"></div>
<script>
(function () {
  var frame = document.querySelector("iframe");
  var pad = document.getElementById("pad");
  var hint = document.getElementById("hint");
  var chain = Promise.resolve();
  function send(body) {
    chain = chain.then(function () {
      return fetch("/api/session/input", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      }).then(function (response) {
        if (!response.ok) hint.textContent = "Attends que Power BI soit affiché, puis réessaie.";
      });
    }).catch(function () {
      hint.textContent = "Attends que Power BI soit affiché, puis réessaie.";
    });
  }
  function picturePoint(event) {
    var doc = frame.contentDocument;
    var canvases = doc ? doc.querySelectorAll("canvas") : [];
    var canvas = null;
    for (var i = 0; i < canvases.length; i++) {
      var candidate = canvases[i];
      if (!canvas || candidate.width * candidate.height > canvas.width * canvas.height) canvas = candidate;
    }
    if (!canvas || !canvas.width || !canvas.height) return null;
    var shown = canvas.getBoundingClientRect();
    var frameBox = frame.getBoundingClientRect();
    var x = (event.clientX - frameBox.left - shown.left) * (canvas.width / shown.width);
    var y = (event.clientY - frameBox.top - shown.top) * (canvas.height / shown.height);
    if (x < 0 || y < 0 || x > canvas.width || y > canvas.height) return null;
    return { x: x, y: y };
  }
  pad.addEventListener("pointerdown", function (event) {
    pad.focus();
    var point = picturePoint(event);
    if (!point) return;
    send({ kind: "click", x: point.x, y: point.y });
  });
  function sendKey(event, down) {
    var text = event.key && event.key.length === 1 ? event.key : "";
    send({ kind: "key", key: event.key || "", code: event.code || "", keyCode: event.keyCode || 0, down: down, text: text });
  }
  pad.addEventListener("keydown", function (event) {
    event.preventDefault();
    sendKey(event, true);
  });
  pad.addEventListener("keyup", function (event) {
    event.preventDefault();
    sendKey(event, false);
  });
  pad.focus();
})();
</script>
</body>
</html>
"""
    return page.replace("__SRC__", src).encode("utf-8")


def _reply(conn: socket.socket, status: str, body: bytes, content_type: str) -> None:
    head = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode()
    conn.sendall(head + body)


def _handle(client: socket.socket) -> None:
    upstream = None
    try:
        _nodelay(client)
        client.settimeout(30)
        got = _read_headers(client)
        if got is None:
            client.close()
            return
        header, rest = got
        request = header.split(b"\r\n", 1)[0].split(b" ")
        raw = request[1].split(b"?", 1)[0] if len(request) > 1 else b"/"
        if raw in (b"/vnc/package.json", b"/package.json"):
            _reply(client, "200 OK", b'{"name":"novnc","version":"1.3.0"}\n', "application/json")
            client.close()
            return
        if request[:1] == [b"GET"] and raw == b"/vnc/vnc.html":
            _reply(client, "200 OK", _vnc_shell(header), "text/html; charset=utf-8")
            client.close()
            return
        target, head, upgrade = _rewrite(header)
        if upgrade:
            body = rest
        else:
            need = _content_length(header)
            while len(rest) < need:
                chunk = client.recv(65536)
                if not chunk:
                    break
                rest += chunk
            body = rest[:need]
        upstream = socket.create_connection(target, timeout=10)
        _nodelay(upstream)
        upstream.sendall(head + body)
        client.settimeout(None)
        upstream.settimeout(None)
        if upgrade:
            _tunnel(client, upstream)
            upstream = None
            return
        while True:
            chunk = upstream.recv(65536)
            if not chunk:
                break
            client.sendall(chunk)
    except OSError:
        pass
    finally:
        for sock in (upstream, client):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass


def main() -> None:
    port = int(os.environ.get("PORT") or "8080")
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(50)
    print(f"Open http://0.0.0.0:{port}  (sign-in screen at /vnc/)", flush=True)
    while True:
        client, _ = server.accept()
        threading.Thread(target=_handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
