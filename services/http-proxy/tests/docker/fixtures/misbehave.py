#!/usr/bin/env python3
"""A site VM with faults, for the robustness tests.

Separate from upstream.py, because it must NOT speak valid HTTP and so cannot use
http.server. It listens on [::]:80 and picks a failure mode from the Host header,
thus one container serves both and a test selects by the subdomain it maps here.

A bad upstream must make the proxy give a gateway error or close cleanly. It must
never crash, wedge, or pass the garbage through as a 200.

	*garbage*      data that is not HTTP, then close. nginx gives a 502.
	*truncated*    Content-Length: 100 but 3 bytes, then close. The client read
	               fails.
	anything else  the same as garbage.
"""

import socket
import threading


def handle(conn: socket.socket) -> None:
    try:
        data = conn.recv(65536)
        host = ""
        for line in data.split(b"\r\n"):
            if line.lower().startswith(b"host:"):
                host = line.split(b":", 1)[1].strip().lower().decode("latin1")
                break
        if "truncated" in host:
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nABC")
        else:
            conn.sendall(b"GARBAGE NOT HTTP\r\n\r\nstill garbage")
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main() -> None:
    srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("::", 80))
    srv.listen(64)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
