from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from secrets import compare_digest
from typing import Optional
from urllib.parse import parse_qs

from . import messages, protocol
from .config import TOKEN_VARIABLE, read_setting, validate_token
from .files import BINARY_READ_MODE, check_root, copy_bytes, file_parts, file_path
from .files import folder_manifest, list_folder


LISTEN_ADDRESS = "0.0.0.0"
REQUEST_HEADER_ENCODING = "latin-1"
RESPONSE_ENCODING = "ascii"


class BuckLANServer(ThreadingHTTPServer):
    root: Optional[Path] = None
    authorization: bytes = b""


class BuckLANHandler(BaseHTTPRequestHandler):
    timeout = protocol.NETWORK_TIMEOUT

    # Спрямовує запит до перевірки з’єднання, списку або файла.
    def do_GET(self) -> None:
        if self.path == protocol.PING_PATH:
            self.reply(200, protocol.PING_RESPONSE)
            return

        root = self.server.root
        route, _, query = self.path.partition(protocol.QUERY_SEPARATOR)
        endpoint = route if route == protocol.FILE_PATH else self.path

        file_endpoints = (protocol.LIST_PATH, protocol.MANIFEST_PATH, protocol.FILE_PATH)

        if root is None or endpoint not in file_endpoints:
            self.reply(404)
            return

        header = self.headers.get(protocol.AUTHORIZATION_HEADER, "")
        presented_token = header.encode(REQUEST_HEADER_ENCODING)

        if not compare_digest(presented_token, self.server.authorization):
            self.reply(401)
            return

        if endpoint == protocol.FILE_PATH:
            self.send_file(root, query)
        else:
            self.send_listing(root, include_hashes=endpoint == protocol.MANIFEST_PATH)

    # Надсилає список файлів і папок та за потреби додає хеші.
    def send_listing(self, root: Path, *, include_hashes: bool) -> None:
        try:
            listing = folder_manifest(root) if include_hashes else list_folder(root)
        except (OSError, ValueError):
            self.reply(500)
            return

        body = json.dumps(listing).encode(RESPONSE_ENCODING)
        self.reply(200, body, protocol.JSON_CONTENT_TYPE)

    # Перевіряє шлях запиту й надсилає вміст одного звичайного файла.
    def send_file(self, root: Path, query: str) -> None:
        try:
            parameters = parse_qs(query, strict_parsing=True, errors="strict", max_num_fields=1)
            parts = file_parts(parameters[protocol.FILE_PARAMETER][0])
        except (KeyError, ValueError):
            self.reply(400)
            return

        try:
            source = file_path(root, parts).open(BINARY_READ_MODE)
        except (FileNotFoundError, NotADirectoryError, ValueError):
            self.reply(404)
            return
        except OSError:
            self.reply(500)
            return

        with source:
            try:
                size = os.fstat(source.fileno()).st_size
            except OSError:
                self.reply(500)
                return

            self.reply_headers(200, size, protocol.FILE_CONTENT_TYPE)

            try:
                copy_bytes(source, self.wfile, size)
            except (OSError, EOFError):
                self.close_connection = True

    # Надсилає HTTP-статус і тіло відповіді з указаним типом вмісту.
    def reply(
        self, status: int, body: bytes = b"", content_type: str = protocol.TEXT_CONTENT_TYPE
    ) -> None:
        self.reply_headers(status, len(body), content_type)
        self.wfile.write(body)

    # Надсилає статус, тип і розмір відповіді перед її вмістом.
    def reply_headers(self, status: int, size: int, content_type: str) -> None:
        self.send_response(status)
        self.send_header(protocol.CONTENT_TYPE_HEADER, content_type)
        self.send_header(protocol.CONTENT_LENGTH_HEADER, str(size))
        self.end_headers()


# Запускає HTTP-сервер і за наявності токена відкриває доступ до файлів.
def serve(port: int, env_file: Path) -> None:
    token = read_setting(TOKEN_VARIABLE, env_file)
    root = None
    authorization = b""

    if token:
        header = protocol.BEARER_TOKEN.format(token=validate_token(token))
        authorization = header.encode(REQUEST_HEADER_ENCODING)
        root = check_root(env_file)

    try:
        with BuckLANServer((LISTEN_ADDRESS, port), BuckLANHandler) as server:
            server.root = root
            server.authorization = authorization
            print(messages.SERVER_STARTED.format(port=port), flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        print(messages.SERVER_STOPPED)
    except OSError as error:
        raise ValueError(messages.SERVER_START_ERROR.format(port=port, error=error)) from error
