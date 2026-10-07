"""Loopback-only, capability-protected image previews owned by this process."""

import atexit
import asyncio
import functools
import html
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import mimetypes
import os
from pathlib import Path
import secrets
import stat
import threading
from urllib.parse import quote, unquote, urlsplit

from .. import mcp
from ..core.security import MAX_FILE_BYTES, validate_path

_IMAGE_TYPES = {'.png', '.gif', '.jpg', '.jpeg', '.webp', '.bmp', '.ico'}
_servers: dict[int, tuple[ThreadingHTTPServer, threading.Thread]] = {}


def _open_relative(root: Path, parts: list[str]) -> int:
    """Open components without following symlinks (openat on POSIX)."""
    if any(part in {'.', '..'} or '\\' in part or '\0' in part for part in parts):
        raise PermissionError('Unsafe path')
    if os.name == 'posix':
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(root, directory_flags)
        try:
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if index < len(parts) - 1:
                    flags |= os.O_DIRECTORY
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    # Windows does not expose openat/O_NOFOLLOW. Refuse symlinks/junctions
    # and check the resolved path before opening the file.
    path = root
    for part in parts:
        path /= part
        if path.is_symlink() or path.is_junction():
            raise PermissionError('Links are not served')
    if not path.resolve().is_relative_to(root):
        raise PermissionError('Outside preview directory')
    return os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0))


class _PreviewHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, root: Path, token: str, **kwargs):
        self.root = root
        self.token = token
        super().__init__(*args, directory=str(root), **kwargs)

    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; img-src 'self'")
        super().end_headers()

    def send_head(self):
        port = self.server.server_address[1]
        if self.headers.get('Host') not in {f'127.0.0.1:{port}', f'localhost:{port}'}:
            self.send_error(403)
            return None
        url = urlsplit(self.path)
        path = unquote(url.path)
        prefix = f'/{self.token}/'
        if url.scheme or url.netloc or not path.startswith(prefix):
            self.send_error(403)
            return None
        parts = [part for part in path[len(prefix):].split('/') if part]
        descriptor = None
        try:
            descriptor = _open_relative(self.root, parts)
            info = os.fstat(descriptor)
            if stat.S_ISDIR(info.st_mode):
                if os.name == 'posix':
                    entries = os.scandir(descriptor)
                else:
                    entries = os.scandir(self.root.joinpath(*parts))
                links = []
                with entries:
                    for entry in sorted(entries, key=lambda entry: entry.name):
                        if entry.is_symlink() or (os.name == 'nt' and Path(entry.path).is_junction()):
                            continue
                        directory = entry.is_dir(follow_symlinks=False)
                        if not directory and (not entry.is_file(follow_symlinks=False) or Path(entry.name).suffix.lower() not in _IMAGE_TYPES):
                            continue
                        target = prefix + '/'.join(quote(part, safe='') for part in parts + [entry.name])
                        if directory:
                            target += '/'
                        links.append(f'<li><a href="{html.escape(target, quote=True)}">{html.escape(entry.name)}</a></li>')
                body = ('<!doctype html><meta charset="utf-8"><title>Sprite previews</title><ul>' + ''.join(links) + '</ul>').encode('utf-8')
                file = io.BytesIO(body)
                content_type = 'text/html; charset=utf-8'
                length = len(body)
            else:
                if not stat.S_ISREG(info.st_mode) or not parts or Path(parts[-1]).suffix.lower() not in _IMAGE_TYPES or info.st_size > MAX_FILE_BYTES:
                    raise PermissionError('Only bounded image files are served')
                file = os.fdopen(descriptor, 'rb')
                descriptor = None
                content_type = mimetypes.guess_type(parts[-1])[0] or 'application/octet-stream'
                length = info.st_size
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(length))
            self.end_headers()
            return file
        except (OSError, ValueError):
            self.send_error(404)
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _close(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _close_all():
    for server, thread in list(_servers.values()):
        _close(server, thread)
    _servers.clear()


atexit.register(_close_all)


@mcp.tool()
async def start_preview_server(directory: str, port: int = 8000) -> str:
    """Preview exported images on loopback, using the returned private URL.

    Only image files and image-directory listings are served; links are
    refused. Servers stop when this MCP process exits. No PID files are used.
    """
    if not 1 <= port <= 65535:
        return 'Invalid port: must be 1..65535'
    root = Path(validate_path(directory))
    if not root.is_dir():
        return f'Directory {directory} not found'
    if port in _servers:
        return f'Preview server already running on port {port}'
    token = secrets.token_urlsafe(32)
    handler = functools.partial(_PreviewHandler, root=root, token=token)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), handler)
    except OSError as error:
        return f'Failed to start preview server: {error}'
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
    try:
        thread.start()
    except BaseException:
        server.server_close()
        raise
    _servers[port] = (server, thread)
    return f'Preview server started: http://127.0.0.1:{port}/{token}/'


@mcp.tool()
async def stop_preview_server(port: int = 8000) -> str:
    """Stop a preview server created by this MCP process."""
    if not 1 <= port <= 65535:
        return 'Invalid port: must be 1..65535'
    entry = _servers.pop(port, None)
    if entry is None:
        return f'No preview server running on port {port}'
    await asyncio.to_thread(_close, *entry)
    return f'Preview server stopped on port {port}'
