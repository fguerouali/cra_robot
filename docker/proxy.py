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


def _reply(conn: socket.socket, status: str, body: bytes, content_type: str) -> None:
    head = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode()
    conn.sendall(head + body)


def _handle(client: socket.socket) -> None:
    upstream = None
    try:
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
