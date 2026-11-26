from contextlib import nullcontext
from errno import EEXIST, ENOTEMPTY, EXDEV
import json
import os
from pathlib import Path
import re
from shutil import rmtree
import stat
import sys
from tempfile import TemporaryDirectory, mkdtemp
from time import sleep
from typing import Optional

from . import messages, protocol
from .client import fetch_file, get_response, parse_ipv4
from .config import TOKEN_VARIABLE, read_setting, validate_token
from .files import (
    BINARY_READ_MODE, PATH_SEPARATOR, SYNC_TEMP_PREFIX, check_root, directory_path,
    file_parts, file_path, is_link, save_file,
)
from .sync_plan import Replacement, SyncPlan, entry_version, path_key, plain_contents, plan_sync


SHA256_PATTERN = re.compile("[0-9a-f]{64}")
BACKUP_NAME = "previous"
WATCH_INTERVAL = 15


# Отримує список для синхронізації та перевіряє його структуру й хеші.
def read_manifest(address: str, port: int, token: str) -> tuple[list[str], dict[str, str]]:
    status, body = get_response(address, port, protocol.MANIFEST_PATH, token=token)

    if status == 401:
        raise ValueError(messages.TOKEN_REJECTED.format(key=TOKEN_VARIABLE))

    if status != 200:
        raise ValueError(messages.MANIFEST_REQUEST_ERROR.format(status=status))

    try:
        manifest = json.loads(body)
        directories = manifest[protocol.DIRECTORIES_KEY]
        files = manifest[protocol.FILES_KEY]
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError(messages.MANIFEST_INVALID_FORMAT) from error

    if not isinstance(directories, list) or not isinstance(files, dict):
        raise ValueError(messages.MANIFEST_INVALID_FORMAT)

    if any(not isinstance(path, str) for path in directories):
        raise ValueError(messages.MANIFEST_INVALID_FORMAT)

    for digest in files.values():
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            raise ValueError(messages.MANIFEST_INVALID_FORMAT)

    return sorted(directories), files


# Перевіряє всі шляхи до запису й виявляє збіги між іменами та типами записів.
def manifest_paths(directories: list[str], files: dict[str, str]) -> dict[str, tuple[str, ...]]:
    paths = {}
    names = {}
    directory_names = set(directories)

    for path in (*directories, *files):
        try:
            parts = file_parts(path)
        except ValueError as error:
            raise ValueError(messages.SYNC_INVALID_PATH.format(path=path)) from error

        name = path_key(path)

        if name in names:
            message = messages.SYNC_PATH_COLLISION.format(first=names[name], second=path)
            raise ValueError(message)

        if len(parts) > 1 and PATH_SEPARATOR.join(parts[:-1]) not in directory_names:
            raise ValueError(messages.MANIFEST_INVALID_FORMAT)

        names[name] = path
        paths[path] = parts

    return paths


# Видаляє запланований запис, якщо це той самий файл або звичайна порожня папка.
def delete_extra(root: Path, path: Path, previous: os.stat_result) -> bool:
    is_directory = stat.S_ISDIR(previous.st_mode)

    try:
        directory_path(root, path.parent.relative_to(root).parts)
        current = path.lstat()
        same_type = stat.S_IFMT(previous.st_mode) == stat.S_IFMT(current.st_mode)

        if is_link(current) or not same_type or not os.path.samestat(previous, current):
            raise ValueError(messages.SYNC_TARGET_CHANGED.format(path=path))

        if is_directory:
            path.rmdir()
        else:
            previous_version = (previous.st_mtime_ns, previous.st_size)
            current_version = (current.st_mtime_ns, current.st_size)

            if previous_version != current_version:
                raise ValueError(messages.SYNC_TARGET_CHANGED.format(path=path))

            path.unlink()
    except FileNotFoundError:
        return False
    except OSError as error:
        if is_directory and error.errno in (ENOTEMPTY, EEXIST):
            return False

        raise ValueError(messages.SYNC_DELETE_ERROR.format(path=path, error=error)) from error

    return True


# Переносить запис через тимчасове ім’я та повертає його за помилки заміни.
def move_into_place(previous: Path, target: Path, staged: Optional[Path] = None) -> None:
    holder = Path(mkdtemp(prefix=SYNC_TEMP_PREFIX, dir=previous.parent))
    backup = holder / BACKUP_NAME

    try:
        previous.rename(backup)
    except OSError:
        holder.rmdir()
        raise

    try:
        if entry_exists(target):
            raise ValueError(messages.SYNC_TARGET_CHANGED.format(path=target))

        replacement = backup if staged is None else staged
        replacement.rename(target)
    except (OSError, ValueError, KeyboardInterrupt):
        try:
            if entry_exists(previous):
                raise FileExistsError(previous)

            backup.rename(previous)
        except OSError as error:
            message = messages.SYNC_RESTORE_ERROR.format(path=previous, backup=backup)
            raise ValueError(message) from error

        holder.rmdir()
        raise

    if staged is None:
        holder.rmdir()
    else:
        rmtree(holder)


# Перевіряє наявність запису, зокрема символічного посилання.
def entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False

    return True


# Звіряє попередній запис і ставить на його місце підготовлений файл або папку.
def apply_replacement(root: Path, staged: Path, change: Replacement) -> None:
    previous = change.old.path
    directory_path(root, previous.parent.relative_to(root).parts)
    current = previous.lstat()

    if is_link(current) or entry_version(current) != entry_version(change.old.metadata):
        raise ValueError(messages.SYNC_TARGET_CHANGED.format(path=previous))

    if change.contents is not None and plain_contents(previous) != change.contents:
        raise ValueError(messages.SYNC_TARGET_CHANGED.format(path=previous))

    move_into_place(previous, change.target, staged)


# Встановлює підготовлений файл і підтримує окремі томи всередині дзеркала.
def install_file(staged: Path, destination: Path) -> None:
    try:
        staged.replace(destination)
        return
    except OSError as error:
        if error.errno != EXDEV:
            raise

    with staged.open(BINARY_READ_MODE) as source:
        size = os.fstat(source.fileno()).st_size
        save_file(destination, source, size)


# Застосовує підготовлені файли й перейменовує вкладені записи перед їхніми папками.
def apply_plan(root: Path, staging: Path, paths: dict[str, tuple[str, ...]], plan: SyncPlan) -> int:
    for directory in plan.directories:
        directory_path(root, directory.relative_to(root).parts, create=True)

    for change in plan.replacements:
        apply_replacement(root, staging.joinpath(*paths[change.path]), change)

    for path, target in plan.installs:
        destination = file_path(root, target.relative_to(root).parts, create_parents=True)
        install_file(staging.joinpath(*paths[path]), destination)

    removed = sum(delete_extra(root, entry.path, entry.metadata) for entry in plan.deletions)

    for previous, target, is_directory in sorted(
        plan.renames, key=lambda change: len(change[0].parts), reverse=True,
    ):
        parts = previous.relative_to(root).parts

        if is_directory:
            directory_path(root, parts)
        else:
            file_path(root, parts)

        move_into_place(previous, target)

    return removed


# Готує завантаження, повторно звіряє джерело й застосовує зміни до дзеркала.
def sync_folder(address: str, port: int, env_file: Path) -> None:
    root = check_root(env_file)
    token = validate_token(read_setting(TOKEN_VARIABLE, env_file))
    directories, files = read_manifest(address, port, token)
    paths = manifest_paths(directories, files)

    try:
        plan = plan_sync(root, directories, files, paths)
    except OSError as error:
        raise ValueError(messages.SYNC_FOLDER_ERROR.format(error=error)) from error

    staging_context = nullcontext(root)

    try:
        if plan.downloads or plan.staged_directories:
            staging_context = TemporaryDirectory(prefix=SYNC_TEMP_PREFIX, dir=root)

        with staging_context as directory:
            staging = Path(directory)

            for path in plan.staged_directories:
                directory_path(staging, paths[path], create=True)

            for path in plan.downloads:
                fetch_file(address, port, path, staging, token, expected_digest=files[path])

            if plan.deletions or plan.replacements or plan.renames:
                if read_manifest(address, port, token) != (directories, files):
                    raise ValueError(messages.SYNC_SOURCE_CHANGED)

            removed = apply_plan(root, staging, paths, plan)
    except OSError as error:
        raise ValueError(messages.SYNC_APPLY_ERROR.format(error=error)) from error

    print(messages.SYNC_TOTALS.format(
        files=len(plan.downloads),
        directories=len(plan.directories) + len(plan.staged_directories),
        removed=removed,
    ))

    if plan.renames or plan.replacements:
        print(messages.SYNC_STRUCTURAL_TOTALS.format(
            renamed=len(plan.renames), replaced=len(plan.replacements),
        ))


# Послідовно повторює синхронізацію й завершується після Ctrl+C.
def watch_folder(address: str, port: int, env_file: Path) -> None:
    host = parse_ipv4(address)
    print(messages.WATCH_STARTED.format(interval=WATCH_INTERVAL), flush=True)

    try:
        while True:
            try:
                sync_folder(host, port, env_file)
            except ValueError as error:
                message = messages.WATCH_RETRY.format(error=error, interval=WATCH_INTERVAL)
                print(message, file=sys.stderr)

            sleep(WATCH_INTERVAL)
    except KeyboardInterrupt:
        print(messages.WATCH_STOPPED)
