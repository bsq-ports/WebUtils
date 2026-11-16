"""Exercise the built Android library on a connected device against local fixtures."""
import argparse
import gzip
import hashlib
import http.server
import json
import os
from pathlib import Path
import shlex
import socket
import socketserver
import ssl
import subprocess
import threading
import time
import urllib.parse
import zipfile
import zlib

import brotli
import h2.config
import h2.connection
import h2.events
import zstandard

ROOT = Path(__file__).resolve().parents[2]
REQUESTS = []
LOCK = threading.Lock()
RETRIES = 0


def response(method, path, headers, body, protocol, tls):
    global RETRIES
    with LOCK:
        REQUESTS.append(dict(method=method, path=path, protocol=protocol, tls=tls, time=time.monotonic()))
    route = urllib.parse.urlsplit(path).path
    status, content, extra = 200, b"webutils-network", {"x-test": "present"}
    if route == "/protocol":
        content = protocol.encode()
    elif route == "/binary":
        content = bytes([0, 1, 127, 128, 255])
    elif route == "/empty":
        status, content = 204, b""
    elif route == "/missing":
        status = 404
    elif route == "/error":
        status = 500
    elif route in ("/redirect", "/redirect307"):
        status, content = (302 if route == "/redirect" else 307), b""
        extra["location"] = "/ok" if route == "/redirect" else "/echo"
    elif route.startswith("/inspect"):
        content = (path + "\n" + headers.get("x-custom", "") + "\n" + headers.get("user-agent", "")).encode()
    elif route == "/echo":
        content = body
    elif route == "/large":
        content = b"x" * 262144
    elif route == "/slow":
        time.sleep(2)
    elif route == "/retry":
        with LOCK:
            RETRIES += 1
            status = 429 if RETRIES == 1 else 200
    elif route.startswith("/compress/"):
        encoding = route.rsplit("/", 1)[1]
        content = {"gzip": gzip.compress, "deflate": zlib.compress, "br": brotli.compress,
                   "zstd": zstandard.ZstdCompressor().compress}[encoding](content)
        extra["content-encoding"] = encoding
    return status, content, extra


class HttpHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def handle_request(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        tls = self.connection.version() if isinstance(self.connection, ssl.SSLSocket) else None
        status, content, headers = response(self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body, "http/1.1", tls)
        try:
            self.send_response(status)
            self.send_header("Content-Length", str(len(content)))
            for k, v in headers.items():
                self.send_header(k.title(), v)
            self.end_headers()
            self.wfile.write(content)
        except ConnectionError:
            pass  # The timeout test deliberately closes its connection.

    do_GET = handle_request
    do_POST = handle_request


class H2Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with self.server.context.wrap_socket(self.request, server_side=True) as sock:
                sock.settimeout(10)
                if sock.selected_alpn_protocol() != "h2":
                    return  # A client that does not request h2 must fail the negotiation test.
                conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False, header_encoding="utf-8"))
                conn.initiate_connection()
                sock.sendall(conn.data_to_send())
                streams = {}
                while data := sock.recv(65536):
                    for event in conn.receive_data(data):
                        if isinstance(event, h2.events.RequestReceived):
                            streams[event.stream_id] = [dict(event.headers), bytearray()]
                        elif isinstance(event, h2.events.DataReceived):
                            streams[event.stream_id][1].extend(event.data)
                            conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                        elif isinstance(event, h2.events.StreamEnded):
                            headers, body = streams.pop(event.stream_id)
                            status, content, extra = response(headers[":method"], headers[":path"], headers, bytes(body), "h2", sock.version())
                            conn.send_headers(event.stream_id, [(":status", str(status)), ("content-length", str(len(content))), *extra.items()])
                            conn.send_data(event.stream_id, content, end_stream=True)
                    sock.sendall(conn.data_to_send())
        except (ssl.SSLError, ConnectionError, TimeoutError, OSError):
            pass


class H2Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def execute(args, **kwargs):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs)
    if result.returncode:
        raise RuntimeError(f"Command failed: {args[0]}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--ndk", type=Path, required=True)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial")
    parser.add_argument("--openssl", default="openssl")
    parser.add_argument("--in-game", action="store_true", help="Run inside Beat Saber; requires a rooted test headset")
    args = parser.parse_args()
    out = ROOT / "build/network-tests"
    out.mkdir(parents=True, exist_ok=True)
    adb = [args.adb] + (["-s", args.serial] if args.serial else [])
    host = "windows-x86_64" if os.name == "nt" else "linux-x86_64"
    tools = args.ndk / "toolchains/llvm/prebuilt" / host / "bin"
    compiler = tools / ("clang++.exe" if os.name == "nt" else "clang++")
    probe = out / ("libwebutils-network-probe.so" if args.in_game else "probe")
    flags = ["--target=aarch64-linux-android24", "-std=c++20", "-O2", "-g", "-fdeclspec", "-static-libstdc++",
             "-DWEBUTILS_NO_JSON", "-DWEBUTILS_NO_BSML", "-I"+str(ROOT/"shared"), "-L"+str(ROOT/"build"),
             "-L"+str(ROOT/"extern/libs"), "-Wl,-rpath-link,"+str(ROOT/"extern/libs")]
    if args.in_game:
        flags += ["-shared", "-fPIC", "-DWEBUTILS_GAME_PROBE"]
    log = execute([compiler, *flags, ROOT/"tests/network/probe.cpp", "-lweb-utils", "-llog", "-o", probe])
    (out/"compile.log").write_text(log)
    execute([args.openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
             "-keyout", out/"key.pem", "-out", out/"cert.pem", "-subj", "/CN=localhost",
             "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"])
    servers, ports = [], []
    device = "/data/local/tmp/webutils-network-tests"
    def shell(*parts):
        return execute([*adb, "shell", shlex.join(map(str, parts))], timeout=120).strip()
    try:
        for kind in ("http", "tls12", "tls13", "h2"):
            context = None
            if kind != "http":
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(out/"cert.pem", out/"key.pem")
                context.set_alpn_protocols(["h2"] if kind == "h2" else ["http/1.1"])
                if kind in ("tls12", "tls13"):
                    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2 if kind == "tls12" else ssl.TLSVersion.TLSv1_3
            if kind == "h2":
                server = H2Server(("127.0.0.1", 0), H2Handler)
                server.context = context
            else:
                server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HttpHandler)
                server.daemon_threads = True
                if context:
                    server.socket = context.wrap_socket(server.socket, server_side=True)
            servers.append(server)
            ports.append(server.server_address[1])
            threading.Thread(target=server.serve_forever, daemon=True).start()
            execute([*adb, "reverse", f"tcp:{ports[-1]}", f"tcp:{ports[-1]}"])
        endpoints = [f"http://127.0.0.1:{ports[0]}", *[f"https://127.0.0.1:{p}" for p in ports[1:]]]
        if args.in_game:
            run_in_game(adb, shell, out, probe, endpoints, ports[0])
        else:
            shell("mkdir", "-p", device)
            for source in (probe, ROOT/"build/libweb-utils.so", ROOT/"extern/libs/libcurl.so"):
                execute([*adb, "push", source, device+"/"+source.name])
            shell("chmod", "755", device+"/probe")
            command = f"LD_LIBRARY_PATH={device} {device}/probe " + shlex.join([*endpoints, device+"/file.bin", f"http://[::1]:{ports[0]}"])
            result = subprocess.run([*adb, "shell", command], capture_output=True, text=True, timeout=150)
            (out/"device-result.log").write_text(result.stdout+result.stderr)
            print(result.stdout, flush=True)
            if result.returncode:
                raise RuntimeError("Network tests failed; see build/network-tests/device-result.log")
    finally:
        (out/"requests.json").write_text(json.dumps(REQUESTS, indent=2))
        for port in ports:
            subprocess.run([*adb, "reverse", "--remove", f"tcp:{port}"], capture_output=True)
        for server in servers:
            server.shutdown()
            server.server_close()


def run_in_game(adb, shell, out, probe, endpoints, http_port):
    game = "com.beatgames.beatsaber"
    loader = f"/sdcard/ModData/{game}/Modloader"
    directory = f"/sdcard/ModData/{game}/qpm-webutils-network-tests"
    library = loader+"/libs/libweb-utils.so"
    remote_probe = loader+"/mods/"+probe.name
    backup = out/"in-game-backup"
    backup.mkdir(exist_ok=True)
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    def hashes():
        text = shell("sh", "-c", f"find {loader} -type f -name '*.so' -exec sha256sum {{}} \\;")
        return {line.split(None, 1)[1]: line.split()[0] for line in text.splitlines()}
    before = hashes()
    if remote_probe in before:
        raise RuntimeError("A previous in-game probe exists; inspect it before retrying")
    execute([*adb, "pull", library, backup/"libweb-utils.so"])
    assert digest(backup/"libweb-utils.so") == before[library]
    assert before[loader+"/libs/libcurl.so"] == digest(ROOT/"extern/libs/libcurl.so"), "Installed curl differs from tested dependency"
    candidate = out/"packaged-libweb-utils.so"
    with zipfile.ZipFile(ROOT/"WebUtils.qmod") as archive:
        manifest = json.loads(archive.read("mod.json"))
        assert "libweb-utils.so" in manifest["libraryFiles"]
        candidate.write_bytes(archive.read("libweb-utils.so"))
    assert digest(candidate) == digest(ROOT/"build/libweb-utils.so")
    config = out/"config.txt"
    config.write_text("\n".join([*endpoints, directory+"/file.bin", f"http://[::1]:{http_port}"])+"\n")
    (out/"in-game-before.json").write_text(json.dumps(before, indent=2))
    shell("am", "force-stop", game)
    try:
        shell("mkdir", "-p", directory)
        shell("rm", "-f", directory+"/result.txt")
        execute([*adb, "push", candidate, library])
        execute([*adb, "push", probe, remote_probe])
        execute([*adb, "push", config, directory+"/config.txt"])
        shell("am", "start", "-W", "-n", game+"/com.unity3d.player.UnityPlayerGameActivity")
        deadline = time.monotonic()+150
        report = ""
        while time.monotonic() < deadline:
            result = subprocess.run([*adb, "shell", "cat", directory+"/result.txt"], capture_output=True, text=True)
            report = result.stdout
            if "RESULT" in report:
                break
            time.sleep(2)
        (out/"in-game-result.log").write_text(report)
        print(report, flush=True)
        assert "failed=0" in report and "RESULT" in report, "In-game network suite did not pass"
        pid = shell("pidof", game)
        assert pid.isdigit(), "Beat Saber exited"
        maps = shell("su", "-c", f"cat /proc/{pid}/maps")
        (out/"in-game-maps.txt").write_text(maps)
        for name in ("libweb-utils.so", "libcurl.so", probe.name):
            assert len([line for line in maps.splitlines() if line.endswith('/'+name) and line.split()[2]=='00000000']) == 1
        loaded = shell("su", "-c", f"sha256sum /data/data/{game}/files/libs/libweb-utils.so").split()[0]
        assert loaded == digest(candidate)
        (out/"in-game-validation.json").write_text(json.dumps({"status":"passed", "pid":int(pid),
            "packageVersion":manifest["version"], "librarySha256":loaded, "source":"WebUtils.qmod",
            "singleLoadedWebUtilsAndCurl":True}, indent=2))
    finally:
        shell("am", "force-stop", game)
        execute([*adb, "push", backup/"libweb-utils.so", library])
        shell("rm", "-f", remote_probe)
        assert hashes() == before, "Device library restoration did not match the backup"
        shell("am", "start", "-W", "-n", game+"/com.unity3d.player.UnityPlayerGameActivity")
        print("Original headset libraries restored; temporary probe removed", flush=True)


if __name__ == "__main__":
    main()
