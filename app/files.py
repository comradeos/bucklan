import os
from pathlib import Path, PureWindowsPath
import stat
from tempfile import NamedTemporaryFile
from typing import BinaryIO

from . import messages, protocol
from .config import ROOT_VARIABLE, read_setting


PATH_SEPARATOR = "/"
PARENT_COMPONENTS = (".", "..")
INVALID_FILENAME_CHARACTERS = '<>:"\\|?*'
INVALID_FILENAME_ENDINGS = (".", " ")
FILE_ATTRIBUTES = "st_file_attributes"
TRANSFER_CHUNK_SIZE = 64 * 1024
TEMP_FILE_PREFIX = ".bucklan-"
BINARY_WRITE_MODE = "wb"


# Розбиває відносний шлях на компоненти, придатні для macOS, Windows і Linux.
def file_parts(relative_path: str) -> tuple[str, ...]:
    parts = tuple(relative_path.split(PATH_SEPARATOR))

    for part in parts:
        if not part or part in PARENT_COMPONENTS or part.endswith(INVALID_FILENAME_ENDINGS):
            raise ValueError(messages.INVALID_FILE_PATH)

        if any(ord(character) < 32 or character in INVALID_FILENAME_CHARACTERS for character in part):
            raise ValueError(messages.INVALID_FILE_PATH)

        if PureWindowsPath(part).is_reserved():
            raise ValueError(messages.INVALID_FILE_PATH)

    return parts


# Виявляє символічне посилання або точку повторної обробки Windows.
def is_link(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, FILE_ATTRIBUTES, 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


# Перевіряє компоненти шляху та за потреби створює папки для завантаження.
def file_path(root: Path, parts: tuple[str, ...], *, create_parents: bool = False) -> Path:
    parent = root
    metadata = parent.lstat()

    for name in parts[:-1]:
        if is_link(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(messages.UNSAFE_FILE_PATH.format(path=parent))

        parent = parent / name

        if create_parents:
            parent.mkdir(exist_ok=True)

        metadata = parent.lstat()

    if is_link(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(messages.UNSAFE_FILE_PATH.format(path=parent))

    path = parent / parts[-1]

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if not create_parents:
            raise

        return path

    if is_link(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(messages.UNSAFE_FILE_PATH.format(path=path))

    return path


# Копіює задану кількість байтів і виявляє передчасне завершення потоку.
def copy_bytes(source: BinaryIO, destination: BinaryIO, size: int) -> None:
    remaining = size

    while remaining:
        chunk = source.read(min(remaining, TRANSFER_CHUNK_SIZE))

        if not chunk:
            raise EOFError(messages.INCOMPLETE_TRANSFER)

        destination.write(chunk)
        remaining -= len(chunk)


# Записує потік у тимчасовий файл поруч із цільовим і замінює ціль після запису.
def save_file(path: Path, source: BinaryIO, size: int) -> None:
    temporary = None

    try:
        with NamedTemporaryFile(
            mode=BINARY_WRITE_MODE, dir=path.parent, prefix=TEMP_FILE_PREFIX, delete=False
        ) as output:
            temporary = Path(output.name)
            copy_bytes(source, output, size)

        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# Перевіряє доступність налаштованої папки й повертає її абсолютний шлях.
def check_root(env_file: Path) -> Path:
    value = read_setting(ROOT_VARIABLE, env_file)
    
    if not value:
        raise ValueError(messages.ROOT_UNSET.format(key=ROOT_VARIABLE, env_file=env_file))

    try:
        path = Path(value).expanduser()
        root = (env_file.parent / path).resolve(strict=True)
    
        with os.scandir(root) as entries:
            next(entries, None)
    except FileNotFoundError as error:
        raise ValueError(messages.ROOT_MISSING.format(path=value, key=ROOT_VARIABLE)) from error
    except NotADirectoryError as error:
        message = messages.ROOT_NOT_DIRECTORY.format(path=value, key=ROOT_VARIABLE)
        raise ValueError(message) from error
    except (OSError, RuntimeError, ValueError) as error:
        message = messages.ROOT_OPEN_ERROR.format(path=value, key=ROOT_VARIABLE, error=error)
        raise ValueError(message) from error

    return root


# Збирає відносні шляхи звичайних файлів і папок без переходу за посиланнями.
def list_folder(root: Path) -> dict[str, list[str]]:
    files = []
    directories = []
    pending = [root]

    while pending:
        directory = pending.pop()

        for path in directory.iterdir():
            metadata = path.lstat()

            if is_link(metadata):
                continue

            relative_path = path.relative_to(root).as_posix()

            if stat.S_ISDIR(metadata.st_mode):
                directories.append(relative_path)
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                files.append(relative_path)

    return {protocol.FILES_KEY: sorted(files), protocol.DIRECTORIES_KEY: sorted(directories)}
