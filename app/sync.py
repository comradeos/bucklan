from errno import EEXIST, ENOTEMPTY
import json
import os
from pathlib import Path
import re
import stat
from unicodedata import normalize

from . import messages, protocol
from .client import fetch_file, get_response
from .config import TOKEN_VARIABLE, read_setting, validate_token
from .files import (
    PATH_SEPARATOR, check_root, directory_path, file_digest, file_parts, file_path,
    is_link, walk_folder,
)


SHA256_PATTERN = re.compile("[0-9a-f]{64}")
UNICODE_FORM = "NFC"


# Нормалізує ім’я для порівняння на файлових системах із різним регістром.
def path_key(path: str) -> str:
    return normalize(UNICODE_FORM, path).casefold()


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


# Визначає, які папки створити й які файли завантажити.
def plan_updates(
    root: Path,
    directories: list[str],
    files: dict[str, str],
    paths: dict[str, tuple[str, ...]],
) -> tuple[list[str], list[str]]:
    missing_directories = []
    changed_files = []

    for path in directories:
        try:
            directory_path(root, paths[path])
        except FileNotFoundError:
            missing_directories.append(path)

    for path, expected_digest in sorted(files.items()):
        try:
            target = file_path(root, paths[path])
        except FileNotFoundError:
            changed_files.append(path)
            continue

        if file_digest(target) != expected_digest:
            changed_files.append(path)

    return missing_directories, changed_files


# Запам’ятовує зайві записи, щоб видалити файли перед їхніми папками.
def plan_deletions(
    root: Path, paths: dict[str, tuple[str, ...]]
) -> list[tuple[Path, os.stat_result]]:
    protected_names = {path_key(path) for path in paths}

    for parts in paths.values():
        try:
            existing = root.joinpath(*parts).resolve(strict=True)
        except FileNotFoundError:
            continue

        if existing.is_relative_to(root):
            protected_names.add(path_key(existing.relative_to(root).as_posix()))

    files = []
    directories = []

    for path, metadata in walk_folder(root):
        name = path_key(path.relative_to(root).as_posix())

        if name in protected_names:
            continue

        entry = (path, metadata)

        if stat.S_ISDIR(metadata.st_mode):
            directories.append(entry)
        else:
            files.append(entry)

    directories.sort(key=lambda entry: len(entry[0].parts), reverse=True)
    return files + directories


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


# Оновлює дзеркало та видаляє зайві записи після повторної перевірки джерела.
def sync_folder(address: str, port: int, env_file: Path) -> None:
    root = check_root(env_file)
    token = validate_token(read_setting(TOKEN_VARIABLE, env_file))
    directories, files = read_manifest(address, port, token)
    paths = manifest_paths(directories, files)

    try:
        missing_directories, changed_files = plan_updates(root, directories, files, paths)
        deletions = plan_deletions(root, paths)

        for path in missing_directories:
            directory_path(root, paths[path], create=True)
    except OSError as error:
        raise ValueError(messages.SYNC_FOLDER_ERROR.format(error=error)) from error

    for path in changed_files:
        fetch_file(address, port, path, root, token, expected_digest=files[path])

    removed = 0

    if deletions:
        if read_manifest(address, port, token) != (directories, files):
            raise ValueError(messages.SYNC_SOURCE_CHANGED)

        for path, metadata in deletions:
            removed += delete_extra(root, path, metadata)

    print(messages.SYNC_TOTALS.format(
        files=len(changed_files), directories=len(missing_directories), removed=removed,
    ))
