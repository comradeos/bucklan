from contextlib import contextmanager
from http.client import HTTPConnection, HTTPException, HTTPResponse
from ipaddress import IPv4Address
import json
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urlencode

from . import messages, protocol
from .config import TOKEN_VARIABLE, read_setting, validate_token
from .files import check_root, file_parts, file_path, save_file


# Надсилає GET-запит і тримає з’єднання відкритим, поки клієнт читає відповідь.
@contextmanager
def open_response(
    address: str,
    port: int,
    endpoint: str,
    token: Optional[str] = None,
) -> Iterator[HTTPResponse]:
    try:
        host = str(IPv4Address(address))
    except ValueError as error:
        raise ValueError(messages.INVALID_ADDRESS.format(address=address)) from error

    connection = HTTPConnection(host, port, timeout=protocol.NETWORK_TIMEOUT)
    headers = {}

    if token is not None:
        headers[protocol.AUTHORIZATION_HEADER] = protocol.BEARER_TOKEN.format(token=token)

    try:
        connection.request(protocol.GET_METHOD, endpoint, headers=headers)
    
        with connection.getresponse() as response:
            yield response
    except (OSError, HTTPException) as error:
        message = messages.CONNECTION_ERROR.format(address=host, port=port, error=error)
        raise ValueError(message) from error
    finally:
        connection.close()


# Читає невелику відповідь для перевірки з’єднання або отримання списку.
def get_response(
    address: str,
    port: int,
    endpoint: str,
    token: Optional[str] = None,
    limit: Optional[int] = None,
) -> tuple[int, bytes]:
    with open_response(address, port, endpoint, token) as response:
        return response.status, response.read(limit) if response.status == 200 else b""


# Перевіряє, чи відповідає BuckLAN за заданою адресою.
def ping(address: str, port: int) -> None:
    status, body = get_response(
        address, port, protocol.PING_PATH, limit=len(protocol.PING_RESPONSE) + 1
    )
    
    if status != 200 or body != protocol.PING_RESPONSE:
        raise ValueError(messages.PING_UNEXPECTED_RESPONSE.format(address=address, port=port))
    
    print(messages.PING_OK.format(address=address, port=port))


# Отримує список із сервера й виводить відносні шляхи без керівних символів.
def show_listing(address: str, port: int, env_file: Path) -> None:
    token = validate_token(read_setting(TOKEN_VARIABLE, env_file))

    status, body = get_response(address, port, protocol.LIST_PATH, token=token)
    
    if status == 401:
        raise ValueError(messages.TOKEN_REJECTED.format(key=TOKEN_VARIABLE))
    if status != 200:
        raise ValueError(messages.LIST_REQUEST_ERROR.format(status=status))

    try:
        listing = json.loads(body)
        directories = listing[protocol.DIRECTORIES_KEY]
        files = listing[protocol.FILES_KEY]
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError(messages.LIST_INVALID_FORMAT) from error
    for paths in (directories, files):
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ValueError(messages.LIST_INVALID_FORMAT)

    for path in [messages.DIRECTORY_PATH.format(path=directory) for directory in directories] + files:
        print(path if path.isprintable() else repr(path))
    
    print(messages.LIST_TOTALS.format(files=len(files), directories=len(directories)))


# Завантажує один файл і зберігає його за тим самим відносним шляхом.
def download_file(address: str, port: int, relative_path: str, env_file: Path) -> None:
    parts = file_parts(relative_path)
    root = check_root(env_file)
    token = validate_token(read_setting(TOKEN_VARIABLE, env_file))
    query = urlencode({protocol.FILE_PARAMETER: relative_path})
    endpoint = protocol.QUERY_SEPARATOR.join((protocol.FILE_PATH, query))

    with open_response(address, port, endpoint, token) as response:
        if response.status == 401:
            raise ValueError(messages.TOKEN_REJECTED.format(key=TOKEN_VARIABLE))

        if response.status == 404:
            raise ValueError(messages.FILE_NOT_FOUND.format(path=relative_path))

        if response.status != 200:
            raise ValueError(messages.FILE_REQUEST_ERROR.format(status=response.status))

        length = response.getheader(protocol.CONTENT_LENGTH_HEADER)

        if response.chunked or not length or not length.isascii() or not length.isdecimal():
            raise ValueError(messages.INVALID_FILE_SIZE)

        try:
            size = int(length)
        except ValueError as error:
            raise ValueError(messages.INVALID_FILE_SIZE) from error

        try:
            target = file_path(root, parts, create_parents=True)
            save_file(target, response, size)
        except (OSError, EOFError) as error:
            message = messages.FILE_DOWNLOAD_ERROR.format(path=relative_path, error=error)
            raise ValueError(message) from error

    print(messages.FILE_DOWNLOADED.format(path=target))
