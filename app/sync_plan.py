from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
from typing import Optional
from unicodedata import normalize

from . import messages
from .files import file_digest, is_link, walk_folder


UNICODE_FORM = "NFC"


@dataclass(frozen=True)
class LocalEntry:
    path: Path
    metadata: os.stat_result


@dataclass(frozen=True)
class Replacement:
    path: str
    old: LocalEntry
    target: Path
    contents: Optional[dict[str, tuple[int, ...]]] = None


@dataclass
class SyncPlan:
    directories: list[Path] = field(default_factory=list)
    staged_directories: list[str] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    installs: list[tuple[str, Path]] = field(default_factory=list)
    replacements: list[Replacement] = field(default_factory=list)
    renames: list[tuple[Path, Path, bool]] = field(default_factory=list)
    deletions: list[LocalEntry] = field(default_factory=list)


# Нормалізує шлях для порівняння імен на різних файлових системах.
def path_key(path: str) -> str:
    return normalize(UNICODE_FORM, path).casefold()


# Зберігає ідентичність і час зміни запису без урахування часу читання.
def entry_version(metadata: os.stat_result) -> tuple[int, ...]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_mtime_ns, metadata.st_size)


# Фіксує вміст папки, яку має замінити файл, і перевіряє спеціальні записи.
def plain_contents(root: Path) -> dict[str, tuple[int, ...]]:
    return {
        path.relative_to(root).as_posix(): entry_version(metadata)
        for path, metadata in walk_folder(root, require_plain=True)
    }


# Шукає фактичний локальний запис за іменем, зокрема за коротким іменем Windows.
def matching_entry(target: Path, candidates: list[LocalEntry]) -> Optional[LocalEntry]:
    name = normalize(UNICODE_FORM, target.name)
    exact = [entry for entry in candidates if normalize(UNICODE_FORM, entry.path.name) == name]

    if len(exact) == 1:
        return exact[0]

    if len(candidates) == 1:
        entry = candidates[0]

        try:
            metadata = target.lstat()
        except FileNotFoundError:
            return entry

        if is_link(metadata) or not os.path.samestat(entry.metadata, metadata):
            raise ValueError(messages.SYNC_PATH_COLLISION.format(first=entry.path, second=target))

        return entry

    if candidates:
        message = messages.SYNC_PATH_COLLISION.format(
            first=candidates[0].path, second=candidates[1].path,
        )
        raise ValueError(message)

    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return None

    if is_link(metadata) or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
        raise ValueError(messages.SYNC_SPECIAL_ENTRY.format(path=target))

    return LocalEntry(target.resolve(strict=True), metadata)


# Планує завантаження, заміни типу, перейменування та видалення для одного знімка.
def plan_sync(
    root: Path,
    directories: list[str],
    files: dict[str, str],
    paths: dict[str, tuple[str, ...]],
) -> SyncPlan:
    entries = [LocalEntry(path, metadata) for path, metadata in walk_folder(root)]
    children = {}

    for entry in entries:
        parent = entry.path.parent.relative_to(root).parts
        children.setdefault((parent, path_key(entry.path.name)), []).append(entry)

    plan = SyncPlan()
    working_directories = {(): root}
    staged_parents = set()
    used_entries = set()
    replaced_trees = set()
    directory_names = set(directories)

    for path in (*directories, *sorted(files)):
        parts = paths[path]
        parent_parts = parts[:-1]
        is_directory = path in directory_names

        if parent_parts in staged_parents:
            if is_directory:
                staged_parents.add(parts)
                plan.staged_directories.append(path)
            else:
                plan.downloads.append(path)

            continue

        parent = working_directories[parent_parts]
        target = parent / parts[-1]
        candidates = children.get((parent.relative_to(root).parts, path_key(target.name)), [])
        old = matching_entry(target, candidates)

        if old is None:
            if is_directory:
                plan.directories.append(target)
                working_directories[parts] = target
            else:
                plan.downloads.append(path)
                plan.installs.append((path, target))

            continue

        old_parts = old.path.relative_to(root).parts

        if old_parts in used_entries:
            raise ValueError(messages.SYNC_PATH_COLLISION.format(first=old.path, second=target))

        used_entries.add(old_parts)
        old_is_directory = stat.S_ISDIR(old.metadata.st_mode)

        if old_is_directory != is_directory:
            if is_directory:
                staged_parents.add(parts)
                plan.staged_directories.append(path)
                plan.replacements.append(Replacement(path, old, target))
            else:
                replaced_trees.add(old_parts)
                plan.downloads.append(path)
                plan.replacements.append(Replacement(path, old, target, plain_contents(old.path)))

            continue

        if normalize(UNICODE_FORM, old.path.name) != normalize(UNICODE_FORM, target.name):
            plan.renames.append((old.path, target, is_directory))

        if is_directory:
            working_directories[parts] = old.path
        elif file_digest(old.path) != files[path]:
            plan.downloads.append(path)
            plan.installs.append((path, old.path))

    # Вкладені записи видаляються до їхніх батьківських папок.
    for entry in reversed(entries):
        parts = entry.path.relative_to(root).parts

        if parts in used_entries or any(parts[:depth] in replaced_trees for depth in range(1, len(parts))):
            continue

        plan.deletions.append(entry)

    return plan
