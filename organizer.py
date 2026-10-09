"""A safe, terminal-based file organizer using only Python's standard library."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import logging
import math
import os
import shutil
import stat
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import terminal_ui

APP_NAME = "File Organizer"
APP_VERSION = "1.0.0"
EXIT_OK, EXIT_INVALID, EXIT_PARTIAL, EXIT_FATAL = 0, 2, 3, 4
PROJECT_CONFIG_PATH = Path(__file__).resolve().with_name("config.json")

DEFAULT_CONFIG = {
    "categories": {
        "Images": [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".tiff", ".tif", ".ico", ".heic", ".raw", ".cr2", ".nef", ".arw"],
        "Videos": [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".flv", ".m4v", ".mpg", ".mpeg", ".3gp"],
        "Music": [".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma"],
        "Documents": [".pdf", ".txt", ".doc", ".docx", ".odt", ".rtf", ".md"],
        "Spreadsheets": [".xls", ".xlsx", ".csv", ".ods"],
        "Presentations": [".ppt", ".pptx", ".odp"],
        "Archives": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"],
        "Programs": [".exe", ".msi", ".apk", ".deb", ".appimage"],
    },
    "conflict_policy": "rename",
    "default_category": "Others",
    "include_hidden": False,
    "recursive": False,
    "size_thresholds_mb": {"small": 10, "medium": 100},
    "custom_rules": [],
    "date_organization": {
        "enabled": False,
        "timestamp": "modified",
        "format": "%Y-%m",
    },
    "size_organization": {
        "enabled": False,
        "labels": ["Small", "Medium", "Large"],
    },
    "logging": {"enabled": True, "level": "INFO"},
}


@dataclass(frozen=True)
class Operation:
    source: Path
    destination: Path
    category: str
    rule: str = "extension"
    action: str = "move"
    conflict: bool = False

    def serializable(self) -> dict:
        data = asdict(self)
        data["source"] = str(self.source)
        data["destination"] = str(self.destination)
        return data


@dataclass
class Plan:
    folder: Path
    operations: list[Operation]
    skipped: list[tuple[Path, str]]
    history_error: str | None = None
    runtime_skipped: int = 0


@dataclass(frozen=True)
class Classification:
    """The category and deterministic rule that classified one file."""

    category: str
    rule: str


def app_data_dir() -> Path:
    """Return a per-user data directory without cluttering organized folders."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "FileOrganizer"


def config_path() -> Path:
    """Return the visible configuration file used by normal CLI startup."""
    return PROJECT_CONFIG_PATH


def history_path() -> Path:
    return app_data_dir() / "history.json"


def configure_logging(verbose: bool = False, config: dict | None = None) -> None:
    settings = (config or DEFAULT_CONFIG)["logging"]
    if not settings["enabled"]:
        logging.disable(logging.CRITICAL)
        return
    directory = app_data_dir() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=directory / "organizer.log",
        encoding="utf-8",
        level=logging.DEBUG if verbose else getattr(logging, settings["level"]),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _valid_category_name(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if value in {".", ".."} or value.endswith((".", " ")):
        return False
    if any(ord(char) < 32 or char in '<>:"/\\|?*\x00' for char in value):
        return False
    reserved_names = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in range(1, 10)
    }
    return value.split(".", 1)[0].upper() not in reserved_names


def _normalize_extension(value: object) -> str:
    if not isinstance(value, str) or not value.startswith(".") or len(value) == 1:
        raise ValueError(f"invalid extension: {value!r}; extensions must start with a dot")
    normalized = value.lower()
    if any(char in normalized for char in "\\/\x00") or normalized != normalized.strip() or normalized.count(".") != 1:
        raise ValueError(f"invalid extension: {value!r}")
    return normalized


def _validate_strftime_format(value: str) -> None:
    directives = set("aAbBcdHIjmMpSUwWxXyYZfzGVu%")
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 1 == len(value) or value[index + 1] not in directives:
            directive = value[index:index + 2]
            raise ValueError(f"unsupported date format directive {directive!r}")
        index += 2


def _validate_categories(categories: object) -> dict[str, list[str]]:
    if not isinstance(categories, dict) or not categories:
        raise ValueError("'categories' must be a non-empty object")
    result: dict[str, list[str]] = {}
    seen: dict[str, str] = {}
    for category, extensions in categories.items():
        if not _valid_category_name(category) or not isinstance(extensions, list):
            raise ValueError("each category needs a valid name and an extension list")
        normalized_extensions: list[str] = []
        for extension in extensions:
            normalized = _normalize_extension(extension)
            if normalized in seen:
                raise ValueError(f"duplicate extension {normalized!r} in {category!r} and {seen[normalized]!r}")
            seen[normalized] = category
            normalized_extensions.append(normalized)
        result[category] = normalized_extensions
    return result


def validate_config(config: object) -> dict:
    """Validate and normalize user settings. Unknown keys are rejected safely."""
    if not isinstance(config, dict):
        raise ValueError("configuration root must be a JSON object")
    unknown = set(config) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError(f"unknown configuration setting(s): {', '.join(sorted(unknown))}")
    result = json.loads(json.dumps(DEFAULT_CONFIG))
    result.update(config)
    result["categories"] = _validate_categories(result["categories"])
    if not isinstance(result["conflict_policy"], str) or result["conflict_policy"] not in {"rename", "skip", "overwrite", "ask"}:
        raise ValueError("'conflict_policy' must be rename, skip, overwrite, or ask")
    if not _valid_category_name(result["default_category"]):
        raise ValueError("'default_category' must be a valid category name")
    if not isinstance(result["recursive"], bool) or not isinstance(result["include_hidden"], bool):
        raise ValueError("'recursive' and 'include_hidden' must be true or false")
    limits = result["size_thresholds_mb"]
    if not isinstance(limits, dict) or set(limits) != {"small", "medium"} or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 for v in limits.values()) or limits["small"] >= limits["medium"]:
        raise ValueError("'size_thresholds_mb' needs non-negative small < medium values")
    date = result["date_organization"]
    if not isinstance(date, dict) or set(date) != {"enabled", "timestamp", "format"} or not isinstance(date["enabled"], bool) or not isinstance(date["timestamp"], str) or date["timestamp"] not in {"modified", "created"} or not isinstance(date["format"], str) or not date["format"]:
        raise ValueError("'date_organization' needs enabled, timestamp (modified/created), and format")
    try:
        _validate_strftime_format(date["format"])
        formatted_date = datetime.now().strftime(date["format"])
    except ValueError as error:
        raise ValueError(f"invalid date format: {error}") from error
    if not _valid_category_name(formatted_date):
        raise ValueError("date format must produce a valid single folder name")
    size = result["size_organization"]
    if not isinstance(size, dict) or set(size) != {"enabled", "labels"} or not isinstance(size["enabled"], bool) or not isinstance(size["labels"], list) or len(size["labels"]) != 3 or not all(_valid_category_name(label) for label in size["labels"]):
        raise ValueError("'size_organization' needs enabled and three valid labels")
    logging_settings = result["logging"]
    if not isinstance(logging_settings, dict) or set(logging_settings) != {"enabled", "level"} or not isinstance(logging_settings["enabled"], bool) or not isinstance(logging_settings["level"], str) or logging_settings["level"] not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("'logging' needs enabled and level (DEBUG, INFO, WARNING, or ERROR)")
    rules = result["custom_rules"]
    if not isinstance(rules, list):
        raise ValueError("'custom_rules' must be a list")
    custom_extensions: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"category", "extensions"} or not _valid_category_name(rule["category"]) or not isinstance(rule["extensions"], list):
            raise ValueError("each custom rule needs category and extensions")
        for extension in rule["extensions"]:
            normalized = _normalize_extension(extension)
            if normalized in custom_extensions:
                raise ValueError(f"duplicate extension {normalized!r} in custom rules")
            custom_extensions.add(normalized)
    return result


def merge_config(defaults: dict, supplied: dict) -> dict:
    """Compatibility wrapper for callers using the previous merge function."""
    merged = json.loads(json.dumps(defaults))
    if not isinstance(supplied, dict):
        raise ValueError("configuration root must be a JSON object")
    merged.update(supplied)
    return validate_config(merged)


def load_config(path: Path | None = None) -> dict:
    path = path or config_path()
    try:
        with path.open(encoding="utf-8") as handle:
            return merge_config(DEFAULT_CONFIG, json.load(handle))
    except FileNotFoundError:
        return json.loads(json.dumps(DEFAULT_CONFIG))
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"Configuration error in {path}: {error}") from error


def save_default_config(path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f"{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(DEFAULT_CONFIG, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def normalized_folder(value: str) -> Path:
    candidate = Path(value).expanduser()
    if is_link_or_junction(candidate):
        raise ValueError("Refusing to organize a symbolic-link folder")
    try:
        folder = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise ValueError(f"Folder does not exist: {candidate}") from error
    except PermissionError as error:
        raise ValueError(f"Folder is inaccessible: {candidate}: {error}") from error
    except OSError as error:
        raise ValueError(f"Could not inspect folder: {candidate}: {error}") from error
    if not folder.is_dir():
        raise ValueError(f"Path is not a folder: {folder}")
    return folder


def is_risky_folder(folder: Path) -> bool:
    """Identify broad locations where an accidental organize is especially costly."""
    home = Path.home().resolve()
    filesystem_root = Path(folder.anchor).resolve()
    return folder == filesystem_root or folder == home


def is_hidden(path: Path) -> bool:
    return path.name.startswith(".") or bool(getattr(path.stat(), "st_file_attributes", 0) & 2)


class RuleEngine:
    """Classify files with a predictable priority: custom, extension, fallback."""

    def __init__(self, config: dict):
        self.default_category = config["default_category"]
        self.custom_extensions: dict[str, str] = {}
        self.extension_categories: dict[str, str] = {}
        for rule in config["custom_rules"]:
            for extension in rule["extensions"]:
                self.custom_extensions[_normalize_extension(extension)] = rule["category"]
        for category, extensions in config["categories"].items():
            for extension in extensions:
                self.extension_categories[_normalize_extension(extension)] = category

    def classify(self, path: Path) -> Classification:
        extension = path.suffix.lower()
        if extension in self.custom_extensions:
            return Classification(self.custom_extensions[extension], "custom extension")
        if extension in self.extension_categories:
            return Classification(self.extension_categories[extension], "extension")
        return Classification(self.default_category, "fallback")


def category_for(path: Path, config: dict) -> str:
    """Compatibility helper returning the RuleEngine classification category."""
    return RuleEngine(config).classify(path).category


def is_link_or_junction(path: Path) -> bool:
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_point)


def protected_application_files(folder: Path) -> set[Path]:
    """Keep this application's own source files out of a plan for its folder."""
    application_folder = Path(__file__).resolve().parent
    if folder != application_folder:
        return set()
    return {
        application_folder / "organizer.py",
        application_folder / "file.py",
        application_folder / "terminal_ui.py",
        application_folder / "config.json",
        application_folder / "test_organizer.py",
        application_folder / "README.md",
        application_folder / "CHANGELOG.md",
        application_folder / ".gitignore",
    }


def is_safe_path(root: Path, path: Path) -> bool:
    """Check lexical containment and reject link/reparse-point components."""
    try:
        resolved_root = root.resolve(strict=True)
        candidate = Path(os.path.abspath(path))
        relative = candidate.relative_to(resolved_root)
    except (OSError, ValueError):
        return False
    if is_link_or_junction(root):
        return False
    current = resolved_root
    for component in relative.parts:
        current /= component
        if is_link_or_junction(current):
            return False
    return True


def is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except FileNotFoundError:
        return False


def iter_candidates(folder: Path, recursive: bool, include_hidden: bool, category_names: set[str], *, skip_category_dirs: bool = False) -> Iterable[Path]:
    """Yield regular files, never descending into symlinks or organizer output."""
    if not recursive:
        for path in folder.iterdir():
            if not is_link_or_junction(path) and is_regular_file(path):
                yield path
        return
    def raise_walk_error(error: OSError) -> None:
        raise error

    for current, dirs, names in os.walk(folder, followlinks=False, onerror=raise_walk_error):
        current_path = Path(current)
        dirs[:] = [
            name for name in dirs
            if not is_link_or_junction(current_path / name)
            and (include_hidden or not is_hidden(current_path / name))
            and not (skip_category_dirs and current_path == folder and name in category_names)
        ]
        for name in names:
            path = current_path / name
            if not is_link_or_junction(path) and is_regular_file(path):
                yield path


def destination_for(source: Path, folder: Path, classification: Classification, mode: str, config: dict) -> Path:
    """Plan destination layout after the rule engine has classified a source."""
    base = folder / classification.category
    date_settings = config["date_organization"]
    use_date = mode in {"modified", "created"} or (mode == "extension" and date_settings["enabled"])
    if use_date:
        timestamp = mode if mode in {"modified", "created"} else date_settings["timestamp"]
        value = source.stat().st_mtime if timestamp == "modified" else source.stat().st_ctime
        date_folder = datetime.fromtimestamp(value).strftime(date_settings["format"])
        if not _valid_category_name(date_folder):
            raise ValueError("date format must produce a valid single folder name")
        base /= date_folder
    use_size = mode == "size" or (mode == "extension" and config["size_organization"]["enabled"])
    if use_size:
        megabytes = source.stat().st_size / (1024 * 1024)
        limits = config["size_thresholds_mb"]
        labels = config["size_organization"]["labels"]
        label = labels[0] if megabytes < limits["small"] else labels[1] if megabytes < limits["medium"] else labels[2]
        base /= label
    return base / source.name


def next_destination(destination: Path, reserved: set[Path]) -> Path:
    candidate = destination
    counter = 1
    while os.path.lexists(candidate) or candidate in reserved:
        candidate = destination.with_name(f"{destination.stem}_{counter}{destination.suffix}")
        counter += 1
    return candidate


def move_no_overwrite(source: Path, destination: Path) -> None:
    """Move a regular file without replacing a path created after planning."""
    try:
        os.link(source, destination)
    except FileExistsError:
        raise
    except OSError:
        descriptor: int | None = None
        identity: tuple[int, int] | None = None
        try:
            descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
            details = os.fstat(descriptor)
            identity = details.st_dev, details.st_ino
            with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
                descriptor = None
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            shutil.copystat(source, destination)
            source.unlink()
            return
        except (OSError, KeyboardInterrupt) as error:
            if descriptor is not None:
                os.close(descriptor)
            cleanup_error: OSError | None = None
            if identity is not None:
                try:
                    details = os.stat(destination, follow_symlinks=False)
                    if (details.st_dev, details.st_ino) == identity:
                        destination.unlink()
                except FileNotFoundError:
                    pass
                except OSError as cleanup:
                    cleanup_error = cleanup
            if cleanup_error is not None:
                raise OSError(f"{error}; partial destination remains at {destination}: {cleanup_error}") from error
            raise
    try:
        source.unlink()
    except OSError as error:
        try:
            if os.path.samefile(source, destination):
                destination.unlink()
        except OSError as cleanup:
            raise OSError(f"Move failed and its temporary destination could not be removed: {cleanup}") from error
        raise


def build_plan(folder: Path, config: dict, *, recursive: bool = False, include_hidden: bool = False, mode: str = "extension", conflict_policy: str = "rename") -> Plan:
    names = set(config["categories"]) | {config["default_category"]} | set(config["size_organization"]["labels"])
    operations: list[Operation] = []
    skipped: list[tuple[Path, str]] = []
    reserved: set[Path] = set()
    protected = protected_application_files(folder)
    rules = RuleEngine(config)
    for source in sorted(iter_candidates(folder, recursive, include_hidden, names, skip_category_dirs=True), key=lambda p: str(p).casefold()):
        if source.resolve(strict=False) in protected:
            skipped.append((source, "active application file"))
            continue
        if not is_safe_path(folder, source) or is_link_or_junction(source):
            skipped.append((source, "symbolic link or junction"))
            continue
        if not include_hidden and is_hidden(source):
            skipped.append((source, "hidden file"))
            continue
        if source.parent == folder and source.name in names:
            skipped.append((source, "reserved category name"))
            continue
        try:
            classification = rules.classify(source)
            destination = destination_for(source, folder, classification, mode, config)
        except (OSError, ValueError) as error:
            skipped.append((source, f"cannot inspect file: {error}"))
            continue
        if not is_safe_path(folder, destination):
            skipped.append((source, "destination path contains a symbolic link or junction"))
            continue
        if source.resolve(strict=False) == destination.resolve(strict=False):
            skipped.append((source, "already at destination"))
            continue
        conflict = os.path.lexists(destination) or destination in reserved
        if conflict and conflict_policy == "skip":
            skipped.append((source, "destination already exists"))
            continue
        if conflict and conflict_policy == "rename":
            destination = next_destination(destination, reserved)
            conflict = True
        reserved.add(destination)
        operations.append(Operation(source, destination, classification.category, classification.rule, "move", conflict))
    return Plan(folder, operations, skipped)


def print_plan(plan: Plan) -> None:
    print(terminal_ui.panel(
        "PREVIEW / NO FILES MOVED",
        [
            terminal_ui.status(f"Source: {plan.folder}", "info"),
            f"Planned actions: {len(plan.operations)}   Skipped: {len(plan.skipped)}",
        ],
    ))
    move_rows = [
        [f"{operation.source.name} ->", str(operation.destination.relative_to(plan.folder)), operation.action.upper()]
        for operation in plan.operations[:50]
    ]
    if move_rows:
        print(terminal_ui.table(["SOURCE", "DESTINATION", "ACTION"], move_rows))
    if len(plan.operations) > 50:
        print(terminal_ui.paint(f"... and {len(plan.operations) - 50} additional planned moves", "muted"))
    rows = []
    rows.extend(
        f"{terminal_ui.status(f'{path.name}: {reason}', 'warning')}"
        for path, reason in plan.skipped[:10]
    )
    if len(plan.skipped) > 10:
        rows.append(f"... and {len(plan.skipped) - 10} more skipped files")
    if rows:
        print(terminal_ui.panel("SKIPPED", rows))


def serialize_plan(plan: Plan) -> dict:
    return {
        "folder": str(plan.folder),
        "operations": [operation.serializable() for operation in plan.operations],
        "skipped": [{"path": str(path), "reason": reason} for path, reason in plan.skipped],
    }


def _validated_history_entries(data: object) -> list[dict]:
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict) and data.get("version") == 1 and isinstance(data.get("entries"), list):
        entries = data["entries"]
    else:
        raise ValueError("history must be a version 1 object with an entries list")
    validated: list[dict] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not all(isinstance(entry.get(key), str) and entry[key] for key in ("id", "timestamp", "folder")):
            raise ValueError(f"history entry {index + 1} has invalid metadata")
        if not isinstance(entry.get("operation"), str) or entry["operation"] not in {"organize", "rename"} or not isinstance(entry.get("moves"), list):
            raise ValueError(f"history entry {index + 1} has invalid operation or moves")
        root = Path(entry["folder"]).resolve(strict=False)
        moves: list[dict] = []
        for move_index, move in enumerate(entry["moves"]):
            if not isinstance(move, dict) or not isinstance(move.get("source"), str) or not isinstance(move.get("destination"), str):
                raise ValueError(f"history entry {index + 1}, move {move_index + 1} is invalid")
            source = Path(move["source"])
            destination = Path(move["destination"])
            if not source.is_absolute() or not destination.is_absolute():
                raise ValueError(f"history entry {index + 1}, move {move_index + 1} has a non-absolute path")
            for candidate in (source, destination):
                try:
                    candidate.resolve(strict=False).relative_to(root)
                except ValueError as error:
                    raise ValueError(f"history entry {index + 1} contains a path outside its recorded folder") from error
            moves.append(move)
        normalized = dict(entry)
        normalized["moves"] = moves
        normalized.setdefault("status", "completed")
        if not isinstance(normalized["status"], str) or normalized["status"] not in {"completed", "partially_undone", "undone"}:
            raise ValueError(f"history entry {index + 1} has an invalid status")
        validated.append(normalized)
    return validated


def load_history() -> list[dict]:
    path = history_path()
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        return _validated_history_entries(data)
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        logging.getLogger(__name__).debug("Could not read history", exc_info=True)
        raise ValueError(f"History error in {path}: {error}") from error


def save_history(entries: list[dict]) -> None:
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f"{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"version": 1, "entries": entries}, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def record_history(folder: Path, successful: list[Operation], operation_type: str = "organize") -> None:
    if not successful:
        return
    entries = load_history()
    entries.append({
        "id": datetime.now().strftime("%Y%m%dT%H%M%S%f"),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "folder": str(folder),
        "operation": operation_type,
        "moves": [item.serializable() for item in successful],
    })
    save_history(entries)


def execute_plan(plan: Plan, policy: str, quiet: bool = False, *, yes: bool = False) -> tuple[list[Operation], list[tuple[Operation, str]]]:
    successful: list[Operation] = []
    failures: list[tuple[Operation, str]] = []
    created: set[Path] = set()
    total = len(plan.operations)
    interactive_progress = not quiet and terminal_ui.supports_color()
    for index, operation in enumerate(plan.operations, 1):
        try:
            if not is_safe_path(plan.folder, operation.source) or is_link_or_junction(operation.source) or not operation.source.is_file():
                raise OSError("source is no longer a safe regular path")
            if not is_safe_path(plan.folder, operation.destination.parent):
                raise OSError("destination parent contains a symbolic link or junction")
            if operation.conflict and policy == "ask":
                if not confirm(f"Destination exists for {operation.source.name}. Overwrite? [y/N]: ", yes=yes):
                    plan.runtime_skipped += 1
                    continue
            parent = operation.destination.parent
            if not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
                created.add(parent)
            if not is_safe_path(plan.folder, parent):
                raise OSError("destination folder is a symbolic link or junction")
            if os.path.lexists(operation.destination):
                if not operation.conflict:
                    raise FileExistsError("destination appeared after preview; not overwritten")
                if policy not in {"overwrite", "ask"}:
                    raise FileExistsError("destination appeared after preview; not overwritten")
                if operation.destination.is_dir() or is_link_or_junction(operation.destination):
                    raise OSError("refusing to overwrite a directory, link, or junction")
                os.replace(operation.source, operation.destination)
            else:
                move_no_overwrite(operation.source, operation.destination)
            successful.append(operation)
        except (OSError, shutil.Error) as error:
            failures.append((operation, str(error)))
            print(terminal_ui.paint(f"[ERROR] Could not move {operation.source.name}: {error}", "red", sys.stderr), file=sys.stderr)
            logging.getLogger(__name__).debug("Move failed: %s", operation.source, exc_info=True)
        except KeyboardInterrupt:
            failures.append((operation, "interrupted by user"))
            break
        if interactive_progress:
            terminal_ui.progress("Organizing", index, total)
    for directory in sorted(created, key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        record_history(plan.folder, successful)
    except (OSError, ValueError) as error:
        plan.history_error = str(error)
        print(f"[ERROR] Files moved, but their undo history could not be saved: {error}", file=sys.stderr)
    return successful, failures


def scan(
    folder: Path,
    config: dict,
    recursive: bool,
    include_hidden: bool,
    *,
    on_progress: Callable[[int], None] | None = None,
) -> list[Path]:
    names = set(config["categories"]) | {config["default_category"]} | set(config["size_organization"]["labels"])
    results: list[Path] = []
    for path in iter_candidates(folder, recursive, include_hidden, names):
        if is_link_or_junction(path) or (not include_hidden and is_hidden(path)):
            continue
        results.append(path)
        if on_progress is not None:
            on_progress(len(results))
    return results


def search_files(files: Iterable[Path], config: dict, *, pattern: str = "*", name: str | None = None,
                 extension: str | None = None, category: str | None = None, min_size: int | None = None,
                 max_size: int | None = None, modified_after: datetime | None = None,
                 modified_before: datetime | None = None) -> list[Path]:
    """Filter files without rescanning or reading their contents."""
    normalized_extension = None
    if extension:
        normalized_extension = extension.lower() if extension.startswith(".") else f".{extension.lower()}"
    pattern = pattern.casefold()
    name = name.casefold() if name else None
    results: list[Path] = []
    for path in files:
        try:
            details = path.stat()
        except OSError:
            continue
        if not fnmatch.fnmatchcase(path.name.casefold(), pattern):
            continue
        if name and name not in path.name.casefold():
            continue
        if normalized_extension and path.suffix.lower() != normalized_extension:
            continue
        if category and category_for(path, config).casefold() != category.casefold():
            continue
        if min_size is not None and details.st_size < min_size:
            continue
        if max_size is not None and details.st_size > max_size:
            continue
        modified = datetime.fromtimestamp(details.st_mtime)
        if modified_after and modified < modified_after:
            continue
        if modified_before and modified > modified_before:
            continue
        results.append(path)
    return sorted(results, key=lambda path: str(path).casefold())


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def stats(files: list[Path], config: dict) -> dict:
    counts: Counter[str] = Counter()
    sizes: Counter[str] = Counter()
    extensions: Counter[str] = Counter()
    total = 0
    for path in files:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        category = category_for(path, config)
        counts[category] += 1
        sizes[category] += size
        extensions[path.suffix.lower() or "(none)"] += 1
        total += size
    return {"files_found": sum(counts.values()), "total_size": total, "categories": dict(counts), "category_sizes": dict(sizes), "extensions": dict(extensions)}


def storage_analysis(files: Iterable[Path], config: dict, limit: int = 10) -> dict:
    """Return category totals plus the largest and oldest accessible files."""
    collected = list(files)
    report = stats(collected, config)
    details: list[tuple[Path, os.stat_result]] = []
    for path in collected:
        try:
            details.append((path, path.stat()))
        except OSError:
            continue
    limit = max(0, limit)
    report["largest_files"] = [
        {"path": str(path), "size": item.st_size, "modified": datetime.fromtimestamp(item.st_mtime).isoformat(timespec="seconds")}
        for path, item in sorted(details, key=lambda value: value[1].st_size, reverse=True)[:limit]
    ]
    report["oldest_files"] = [
        {"path": str(path), "size": item.st_size, "modified": datetime.fromtimestamp(item.st_mtime).isoformat(timespec="seconds")}
        for path, item in sorted(details, key=lambda value: value[1].st_mtime)[:limit]
    ]
    return report


def sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def duplicate_groups(files: list[Path]) -> list[dict]:
    by_size: dict[int, list[Path]] = defaultdict(list)
    for path in files:
        try:
            by_size[path.stat().st_size].append(path)
        except OSError:
            continue
    groups: list[dict] = []
    for size, candidates in by_size.items():
        if len(candidates) < 2:
            continue
        by_hash: dict[str, list[Path]] = defaultdict(list)
        for path in candidates:
            try:
                by_hash[sha256(path)].append(path)
            except OSError as error:
                logging.warning("Cannot hash %s: %s", path, error)
        for digest, matched in by_hash.items():
            if len(matched) > 1:
                groups.append({"size": size, "sha256": digest, "files": [str(p) for p in matched], "potential_savings": size * (len(matched) - 1)})
    return groups


def build_rename_plan(folder: Path, files: Iterable[Path], *, prefix: str = "", suffix: str = "",
                      start: int | None = None, digits: int = 3, date_format: str | None = None,
                      date_source: str = "modified") -> Plan:
    """Plan collision-safe file renames. No rename occurs while planning."""
    if not any((prefix, suffix, start is not None, date_format)):
        raise ValueError("choose at least one rename rule: prefix, suffix, numbering, or date")
    if start is not None and start < 0:
        raise ValueError("rename sequence start must be zero or greater")
    if digits < 1 or digits > 12:
        raise ValueError("rename sequence digits must be between 1 and 12")
    invalid_name_chars = '<>:"/\\|?*\x00'
    if any(ord(char) < 32 or char in invalid_name_chars for char in prefix + suffix):
        raise ValueError("rename prefix and suffix cannot contain path separators or invalid filename characters")
    if date_format:
        _validate_strftime_format(date_format)
    operations: list[Operation] = []
    skipped: list[tuple[Path, str]] = []
    ordered = sorted(files, key=lambda path: str(path).casefold())
    sources = {path.resolve(strict=False) for path in ordered}
    reserved: set[Path] = set()
    protected = protected_application_files(folder)
    for position, source in enumerate(ordered):
        if source.resolve(strict=False) in protected:
            skipped.append((source, "active application file"))
            continue
        if not is_safe_path(folder, source) or is_link_or_junction(source):
            skipped.append((source, "symbolic link or path outside the selected folder"))
            continue
        try:
            stamp = ""
            if date_format:
                value = source.stat().st_mtime if date_source == "modified" else source.stat().st_ctime
                stamp = datetime.fromtimestamp(value).strftime(date_format)
                if not stamp or stamp in {".", ".."} or any(char in invalid_name_chars for char in stamp):
                    raise ValueError("date format must produce a valid filename component")
            parts = [prefix, source.stem, suffix]
            if stamp:
                parts.append(stamp)
            if start is not None:
                parts.append(f"{start + position:0{digits}d}")
            destination = source.with_name("_".join(part for part in parts if part) + source.suffix)
            while (os.path.lexists(destination) and destination.resolve(strict=False) != source.resolve(strict=False)) or destination in reserved or destination.resolve(strict=False) in (sources - {source.resolve(strict=False)}):
                destination = next_destination(destination, reserved | sources)
            if not is_safe_path(folder, destination):
                skipped.append((source, "destination path contains a symbolic link or junction"))
                continue
            if destination == source:
                skipped.append((source, "rename would not change the filename"))
                continue
            reserved.add(destination)
            operations.append(Operation(source, destination, "Rename", "rename", "rename", destination.exists()))
        except (OSError, ValueError) as error:
            skipped.append((source, f"cannot inspect file: {error}"))
    return Plan(folder, operations, skipped)


def execute_rename_plan(plan: Plan, quiet: bool = False) -> tuple[list[Operation], list[tuple[Operation, str]]]:
    """Execute exactly the supplied rename plan; never overwrite a new collision."""
    successful: list[Operation] = []
    failures: list[tuple[Operation, str]] = []
    for operation in plan.operations:
        try:
            if not is_safe_path(plan.folder, operation.source) or is_link_or_junction(operation.source) or not operation.source.is_file():
                raise OSError("source is no longer a safe regular path")
            if not is_safe_path(plan.folder, operation.destination):
                raise OSError("destination contains a symbolic link or junction")
            if os.path.lexists(operation.destination):
                raise FileExistsError("destination appeared after preview; not overwritten")
            move_no_overwrite(operation.source, operation.destination)
            successful.append(operation)
        except (OSError, shutil.Error) as error:
            failures.append((operation, str(error)))
            print(terminal_ui.paint(f"[ERROR] Could not rename {operation.source.name}: {error}", "red", sys.stderr), file=sys.stderr)
        except KeyboardInterrupt:
            failures.append((operation, "interrupted by user"))
            break
    try:
        record_history(plan.folder, successful, "rename")
    except (OSError, ValueError) as error:
        plan.history_error = str(error)
        print(f"[ERROR] Files renamed, but their undo history could not be saved: {error}", file=sys.stderr)
    return successful, failures


def undo(operation_id: str | None = None, quiet: bool = False) -> int:
    entries = load_history()
    candidates = [entry for entry in entries if entry.get("operation") in {"organize", "rename"} and entry.get("status") != "undone" and entry.get("moves")]
    entry = next((e for e in reversed(candidates) if e.get("id") == operation_id), None) if operation_id else (candidates[-1] if candidates else None)
    if not entry:
        print("[INFO] No matching organization operation in history.")
        return EXIT_OK
    root = Path(entry["folder"]).resolve(strict=False)
    restored = 0
    failed = 0
    remaining: list[dict] = []
    moves = list(reversed(entry.get("moves", [])))
    for index, move in enumerate(moves):
        source, destination = Path(move["source"]), Path(move["destination"])
        if not is_safe_path(root, source) or not is_safe_path(root, destination) or is_link_or_junction(destination) or not destination.is_file():
            if not quiet:
                print(f"{terminal_ui.icon('warning')} Cannot safely restore; moved file is missing or is a link: {destination}")
            failed += 1
            remaining.append(move)
            continue
        if os.path.lexists(source):
            if not quiet:
                print(f"{terminal_ui.icon('warning')} Cannot restore; original path already exists: {source}")
            failed += 1
            remaining.append(move)
            continue
        try:
            for candidate in (source, destination):
                candidate.resolve(strict=False).relative_to(root)
            parent = source.parent
            while parent != root:
                if is_link_or_junction(parent):
                    raise OSError(f"refusing to restore through a symbolic link or junction: {parent}")
                if root not in parent.parents and parent != root:
                    raise OSError("restore destination is outside the recorded folder")
                parent = parent.parent
            source.parent.mkdir(parents=True, exist_ok=True)
            move_no_overwrite(destination, source)
            restored += 1
            if not quiet:
                print(f"{terminal_ui.icon('success')} Restored {source.name}")
        except (OSError, shutil.Error) as error:
            print(terminal_ui.paint(f"[ERROR] Could not restore {source}: {error}", "red", sys.stderr), file=sys.stderr)
            failed += 1
            remaining.append(move)
        except KeyboardInterrupt:
            failed += 1
            remaining.extend(moves[index:])
            break
    entry["moves"] = list(reversed(remaining))
    entry["status"] = "partially_undone" if remaining else "undone"
    try:
        save_history(entries)
    except OSError as error:
        print(f"[ERROR] Files were restored, but the updated history could not be saved: {error}", file=sys.stderr)
        failed += 1
    if not quiet:
        print(terminal_ui.panel("UNDO SUMMARY", [
            f"Restored: {restored}",
            f"Skipped or failed: {failed}",
            f"Remaining in history: {len(remaining)}",
        ]))
    return EXIT_PARTIAL if failed else EXIT_OK


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("folder", nargs="?", default=".", help="folder to inspect (default: current directory)")
    parser.add_argument("--also", action="append", default=[], metavar="FOLDER", help="additional folder; repeated use is supported")
    parser.add_argument("--recursive", action="store_true", help="include nested folders, excluding organizer output folders")
    parser.add_argument("--include-hidden", action="store_true", help="include hidden files")
    parser.add_argument("--config", type=Path, help="path to JSON configuration")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--quiet", action="store_true", help="reduce normal output")
    parser.add_argument("--allow-risky", action="store_true", help="allow organizing the home or filesystem root folder")
    parser.add_argument("--yes", action="store_true", help="confirm prompts explicitly (for scripts; review destructive actions first)")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Safely preview and organize files by configurable rules.",
        epilog=(
            "Examples:\n"
            "  organizer.py preview . --recursive\n"
            "  organizer.py organize C:\\Users\\you\\Downloads\n"
            "  organizer.py find . \"*.pdf\" --min-size-mb 1\n"
            "  organizer.py undo --yes\n"
            "Preview never moves files. Organize and undo ask before changing files; "
            "--yes is intended for reviewed automation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    root.add_argument("--verbose", action="store_true", help="write detailed diagnostics to the application log")
    root.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    commands = root.add_subparsers(dest="command")
    for name, help_text in (("preview", "show planned moves without changing files"), ("organize", "preview, confirm, and execute moves"), ("scan", "list discovered files"), ("stats", "show file statistics"), ("analyze", "report storage totals, largest, and oldest files"), ("largest", "report the largest files"), ("find", "search files"), ("duplicates", "find exact duplicates by SHA-256"), ("rename", "preview or safely apply batch file renames")):
        sub = commands.add_parser(name, help=help_text)
        add_common_options(sub)
        if name in {"preview", "organize"}:
            sub.add_argument("--by", choices=["extension", "modified", "created", "size"], default="extension", help="organization rule")
            sub.add_argument("--conflict", choices=["rename", "skip", "overwrite", "ask"], help="duplicate-name policy")
        if name == "find":
            sub.add_argument("pattern", nargs="?", default="*", help="filename pattern, e.g. *.pdf")
            sub.add_argument("--name", help="case-insensitive partial filename")
            sub.add_argument("--extension", help="extension, with or without a leading dot")
            sub.add_argument("--category", help="limit results to a category")
            sub.add_argument("--min-size-mb", type=float, help="minimum size in MB")
            sub.add_argument("--max-size-mb", type=float, help="maximum size in MB")
            sub.add_argument("--modified-after", help="modified on/after YYYY-MM-DD")
            sub.add_argument("--modified-before", help="modified on/before YYYY-MM-DD")
        if name == "duplicates":
            sub.add_argument("--min-size-mb", type=float, default=0, help="ignore smaller files")
        if name in {"largest", "analyze"}:
            sub.add_argument("--limit", type=int, default=10, help="maximum number of files to show")
        if name == "largest":
            sub.add_argument("--min-size-mb", type=float, default=0, help="ignore smaller files")
        if name == "rename":
            sub.add_argument("--prefix", default="", help="text before the original filename")
            sub.add_argument("--suffix", default="", help="text after the original filename")
            sub.add_argument("--start", type=int, help="start a numbered sequence")
            sub.add_argument("--digits", type=int, default=3, help="sequence padding width (default: 3)")
            sub.add_argument("--date-format", help="append a strftime date using the selected timestamp")
            sub.add_argument("--date-source", choices=["modified", "created"], default="modified")
            sub.add_argument("--apply", action="store_true", help="ask for confirmation and apply the rename plan")
    history = commands.add_parser("history", help="show operation history")
    history.add_argument("--json", action="store_true")
    undo_parser = commands.add_parser("undo", help="undo the latest recorded organization safely")
    undo_parser.add_argument("operation_id", nargs="?", help="specific history operation ID")
    undo_parser.add_argument("--quiet", action="store_true")
    undo_parser.add_argument("--yes", action="store_true", help="confirm undo without an interactive prompt")
    config = commands.add_parser("config", help="view, validate, or reset configuration")
    config.add_argument("action", choices=["show", "path", "validate", "reset"], nargs="?", default="show")
    config.add_argument("--config", type=Path, help="configuration file to inspect or reset")
    config.add_argument("--yes", action="store_true", help="confirm config reset without an interactive prompt")
    return root


def confirm(prompt: str, *, yes: bool = False, prompt_on_stderr: bool = False) -> bool:
    if yes:
        return True
    try:
        if prompt_on_stderr:
            print(prompt, end="", file=sys.stderr)
            prompt = ""
        return input(prompt).strip().casefold() in {"y", "yes"}
    except EOFError as error:
        raise ValueError("confirmation input is unavailable; rerun with --yes after reviewing the operation") from error


def dashboard_snapshot() -> list[str]:
    """Show only truthful application state that can be read without scanning."""
    try:
        config = load_config()
        category_count = len(config["categories"])
        configuration_status = f"{category_count} categories active"
    except ValueError:
        configuration_status = "Configuration needs attention"
    try:
        history = load_history()
        last = history[-1] if history else None
        if last is None:
            last_operation = "No operations recorded"
        else:
            status = last.get("status", "completed").replace("_", " ")
            last_operation = f"{last['operation']} / {status} / {len(last['moves'])} recorded moves"
    except ValueError:
        last_operation = "History unavailable; inspect with `history`"
    return [
        "Files scanned  Not scanned yet",
        f"Configuration  {configuration_status}",
        f"Last operation  {last_operation}",
        "Current mode  READY / SAFE BY DEFAULT",
    ]


def interactive() -> int:
    menu = [
        ("1", "Organize a folder", "Preview, confirm, move safely", "folder"),
        ("2", "Preview organization", "Plan moves; change nothing", "scan"),
        ("3", "Scan a folder", "Count and list regular files", "scan"),
        ("4", "View folder statistics", "Category counts and storage", "stats"),
        ("5", "Search for files", "Names, patterns, and filters", "search"),
        ("6", "Find large files", "Surface the largest items", "stats"),
        ("7", "Find duplicates", "Compare content; never delete", "duplicate"),
        ("8", "View operation history", "Review recorded operations", "history"),
        ("9", "Undo the last operation", "Restore only when paths are safe", "history"),
        ("10", "Manage configuration", "Review rules and settings", "settings"),
        ("11", "Help and examples", "Commands and safe workflows", "info"),
        ("0", "Exit", "Close File Organizer", "exit"),
    ]
    selected_key = "1"
    command_by_key = {
        "1": "organize", "2": "preview", "3": "scan", "4": "stats",
        "5": "find", "6": "largest", "7": "duplicates",
        "8": "history", "9": "undo", "10": "config",
        "11": "help", "0": "exit",
    }
    while True:
        print(terminal_ui.banner(APP_VERSION))
        print(terminal_ui.panel("SYSTEM STATUS", dashboard_snapshot()))
        menu_rows = [
            [key.rjust(2), terminal_ui.icon(menu_icon), title, description]
            for key, title, description, menu_icon in menu
        ]
        print(terminal_ui.divider("SELECT A WORKFLOW"))
        selected_row = next((index for index, row in enumerate(menu_rows) if row[0].strip() == selected_key), None)
        print(terminal_ui.table(
            ["KEY", "", "WORKFLOW", "DESCRIPTION"],
            menu_rows,
            styles={0: "cyan", 2: "white", 3: "muted"},
            highlight_row=selected_row,
        ))
        print(terminal_ui.divider("READY  ·  TYPE A NUMBER  ·  0 TO EXIT"))
        try:
            choice = input(terminal_ui.paint("\n  › ", "cyan")).strip()
        except EOFError:
            print(f"\n{terminal_ui.status('Input closed; exiting.', 'info')}")
            return EXIT_OK
        if choice == "0":
            return EXIT_OK
        selected_key = choice
        command = command_by_key.get(choice)
        if command == "help":
            parser().print_help()
            continue
        if command in {None, "exit"}:
            print(terminal_ui.status("Choose one of the listed numbers (0-11).", "failure"))
            continue
        selected_label = next((item[1] for item in menu if item[0] == choice), "Exit")
        print(terminal_ui.status(f"Selected workflow: {selected_label}", "success"))
        arguments = [command]
        if command == "config":
            try:
                action = input("Configuration action [show/path/validate/reset] (default show): ").strip().casefold() or "show"
            except EOFError:
                print(terminal_ui.status("Input closed; returning to the menu.", "info"))
                continue
            if action not in {"show", "path", "validate", "reset"}:
                print(f"{terminal_ui.icon('failure')} Choose show, path, validate, or reset.")
                continue
            if action != "show":
                arguments.append(action)
        elif command not in {"history", "undo"}:
            try:
                folder = input("Folder path (e.g. ~/Downloads; blank for current folder): ").strip() or "."
            except EOFError:
                print(terminal_ui.status("Input closed; returning to the menu.", "info"))
                continue
            arguments.append(folder)
            if command == "find":
                try:
                    pattern = input("Filename pattern [*]: ").strip() or "*"
                except EOFError:
                    print(terminal_ui.status("Input closed; returning to the menu.", "info"))
                    continue
                arguments.append(pattern)
            if command in {"organize", "preview"}:
                try:
                    recursive = input("Include subfolders? [y/N]: ").strip().casefold() in {"y", "yes"}
                except EOFError:
                    recursive = False
                if recursive:
                    arguments.append("--recursive")
        try:
            result = main(arguments)
        except (ValueError, OSError) as error:
            print(terminal_ui.status(str(error), "failure"))
            continue
        if result not in {EXIT_OK, EXIT_PARTIAL}:
            print(terminal_ui.status(f"Returned with status {result}.", "warning"))


def emit(value: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, default=str))


def selected_folders(args: argparse.Namespace) -> list[Path]:
    """Normalize folders and reject duplicates or overlapping scan roots."""
    folders = [normalized_folder(value) for value in [args.folder, *args.also]]
    unique: list[Path] = []
    for folder in folders:
        if folder in unique:
            raise ValueError(f"duplicate folder: {folder}")
        for existing in unique:
            try:
                folder.relative_to(existing)
                raise ValueError(f"overlapping folders are not allowed: {folder} is inside {existing}")
            except ValueError as error:
                if "overlapping folders" in str(error):
                    raise
            try:
                existing.relative_to(folder)
                raise ValueError(f"overlapping folders are not allowed: {existing} is inside {folder}")
            except ValueError as error:
                if "overlapping folders" in str(error):
                    raise
        unique.append(folder)
    return unique


def parse_cli_date(value: str | None, option: str) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError(f"{option} must use YYYY-MM-DD") from error


def run(args: argparse.Namespace) -> int:
    if args.command == "config":
        target = args.config or config_path()
        if args.action == "path":
            print(terminal_ui.panel("CONFIGURATION PATH", [str(target)]))
        elif args.action == "validate":
            active_config = load_config(target)
            print(terminal_ui.panel("CONFIGURATION VALID", [
                terminal_ui.status(f"Validated {target}", "success"),
                f"Categories: {len(active_config['categories'])}",
                f"Custom rules: {len(active_config['custom_rules'])}",
                f"Default category: {active_config['default_category']}",
            ]))
        elif args.action == "reset":
            if target.exists():
                if args.yes:
                    answer = "RESET"
                else:
                    try:
                        answer = input(f"This will overwrite {target}. Type RESET to continue: ").strip()
                    except EOFError as error:
                        raise ValueError("confirmation input is unavailable; pass --yes after reviewing the target") from error
                if answer != "RESET":
                    print("[INFO] Configuration was not changed.")
                    return EXIT_OK
            print(terminal_ui.status(f"Wrote default configuration: {save_default_config(target)}", "success"))
        else:
            active_config = load_config(target)
            print(terminal_ui.panel("ACTIVE CONFIGURATION", [
                f"Configuration: {target}",
                f"Default category: {active_config['default_category']}",
                f"Conflict policy: {active_config['conflict_policy']}",
                f"Recursive by default: {'yes' if active_config['recursive'] else 'no'}",
                f"Hidden files included: {'yes' if active_config['include_hidden'] else 'no'}",
                f"Custom rules: {len(active_config['custom_rules'])}",
                f"Date organization: {'enabled' if active_config['date_organization']['enabled'] else 'disabled'}",
                f"Size organization: {'enabled' if active_config['size_organization']['enabled'] else 'disabled'}",
            ]))
            print(terminal_ui.table(
                ["CATEGORY", "EXTENSIONS"],
                [[category, ", ".join(extensions) or "(none)"] for category, extensions in active_config["categories"].items()],
            ))
            if active_config["custom_rules"]:
                print(terminal_ui.table(
                    ["CUSTOM RULE", "EXTENSIONS"],
                    [[rule["category"], ", ".join(rule["extensions"])] for rule in active_config["custom_rules"]],
                ))
        return EXIT_OK
    if args.command == "history":
        entries = load_history()
        if args.json: emit(entries, True)
        elif not entries: print(terminal_ui.status("No history recorded yet.", "info"))
        else:
            print(terminal_ui.panel("OPERATION HISTORY", [
                f"Recorded operations: {len(entries)}",
                "Undo is available only when recorded source/destination paths remain safe.",
            ]))
            print(terminal_ui.table(
                ["ID", "WHEN", "OPERATION", "STATUS", "FILES", "FOLDER"],
                [[
                    entry["id"], entry["timestamp"], entry["operation"],
                    entry.get("status", "completed").replace("_", " "),
                    str(len(entry.get("moves", []))), entry["folder"],
                ] for entry in reversed(entries)],
            ))
        return EXIT_OK
    if args.command == "undo":
        entries = load_history()
        candidates = [entry for entry in entries if entry.get("status") != "undone" and entry.get("moves")]
        entry = next((item for item in reversed(candidates) if item["id"] == args.operation_id), None) if args.operation_id else (candidates[-1] if candidates else None)
        if entry and not args.quiet:
            print(terminal_ui.panel("UNDO REVIEW", [
                f"Operation: {entry['id']} / {entry['operation']}",
                f"Folder: {entry['folder']}",
                f"Moves remaining: {len(entry['moves'])}",
                "Existing originals and unsafe paths will be skipped, never overwritten.",
            ]))
        if entry and not confirm(f"Undo operation {entry['id']} in {entry['folder']} ({len(entry['moves'])} remaining moves)? [y/N]: ", yes=args.yes, prompt_on_stderr=args.json if hasattr(args, "json") else False):
            print(terminal_ui.status("No files were changed.", "info"))
            return EXIT_OK
        return undo(args.operation_id, args.quiet)
    config = load_config(args.config)
    folders = selected_folders(args)
    for folder in folders:
        if is_risky_folder(folder):
            message = f"{folder} is a broad system or home directory"
            if args.command in {"organize", "rename"} and not args.allow_risky:
                raise ValueError(f"Refusing to modify {message}; use --allow-risky after reviewing a preview")
            if not args.quiet:
                print(f"[WARNING] Selected folder {message}.", file=sys.stderr)
    recursive = args.recursive or config["recursive"] or args.command in {"find", "duplicates", "largest", "analyze"}
    hidden = args.include_hidden or config["include_hidden"]
    if args.command in {"preview", "organize"} and not args.json:
        for folder in folders:
            print(terminal_ui.panel(
                "ORGANIZATION TARGET",
                [
                    f"Source directory: {folder}",
                    f"Strategy: {args.by}",
                    f"Scan depth: {'recursive' if recursive else 'top-level only'}",
                    f"Hidden items: {'included' if hidden else 'excluded'}",
                    f"Collision policy: {args.conflict or config['conflict_policy']}",
                    f"Execution mode: {'SAFE PREVIEW' if args.command == 'preview' else 'REVIEW REQUIRED'}",
                ],
            ))
    if args.command in {"preview", "organize"}:
        policy = args.conflict or config["conflict_policy"]
        if args.command == "organize" and args.yes and policy == "ask":
            policy = "overwrite"
        if not args.json and not args.quiet:
            print(terminal_ui.status("Planning file destinations; no files are moved during this stage.", "info"))
        plans = [build_plan(folder, config, recursive=recursive, include_hidden=hidden, mode=args.by, conflict_policy=policy) for folder in folders]
        plan_result = [serialize_plan(plan) for plan in plans]
        if args.json:
            if args.command == "preview":
                emit(plan_result[0] if len(plan_result) == 1 else plan_result, True)
        else:
            for plan in plans:
                if len(plans) > 1: print(f"\nFolder: {plan.folder}")
                print_plan(plan)
        if args.command == "preview":
            return EXIT_OK
        if not any(plan.operations for plan in plans):
            if args.json:
                emit({"plans": plan_result, "status": "no_changes", "moved": 0, "skipped": sum(len(plan.skipped) for plan in plans), "failed": 0}, True)
            return EXIT_OK
        if not args.json:
            print(terminal_ui.panel("CONFIRM ORGANIZATION", [
                f"Source folder(s): {', '.join(str(plan.folder) for plan in plans)}",
                f"Recursive scanning: {'yes' if recursive else 'no'}",
                f"Include hidden files: {'yes' if hidden else 'no'}",
                f"Organization rule: {args.by}",
                f"Collision policy: {policy}",
                f"Planned file moves: {sum(len(plan.operations) for plan in plans)}",
            ]))
        overwrites = sum(operation.conflict for plan in plans for operation in plan.operations) if policy in {"overwrite", "ask"} else 0
        if overwrites:
            print(terminal_ui.panel("OVERWRITE WARNING", [
                f"{overwrites} existing file(s) may be permanently replaced.",
                "Overwritten content is not recoverable through undo.",
                "Use the default rename policy to preserve both copies.",
            ]), file=sys.stderr if args.json else sys.stdout)
            if not args.yes:
                try:
                    if args.json:
                        print("Type OVERWRITE to continue: ", end="", file=sys.stderr)
                        confirmation = input().strip()
                    else:
                        confirmation = input("Type OVERWRITE to continue: ").strip()
                except EOFError as error:
                    raise ValueError("confirmation input is unavailable; use --yes only after reviewing the overwrite warning") from error
                if confirmation != "OVERWRITE":
                    print(f"{terminal_ui.icon('info')} No files were changed.")
                    return EXIT_OK
        if not confirm("Continue with all planned moves? [y/N]: ", yes=args.yes, prompt_on_stderr=args.json):
            if args.json:
                emit({"plans": plan_result, "status": "cancelled", "moved": 0, "skipped": sum(len(plan.skipped) for plan in plans), "failed": 0}, True)
            else:
                print("[INFO] No files were changed.")
            return EXIT_OK
        configure_logging(getattr(args, "verbose", False), config)
        successful, failures, skipped = 0, 0, 0
        started = time.perf_counter()
        categories_absent_before = {
            (plan.folder, operation.category)
            for plan in plans
            for operation in plan.operations
            if not (plan.folder / operation.category).exists()
        }
        completed_categories: set[tuple[Path, str]] = set()
        for plan in plans:
            good, bad = execute_plan(plan, policy, args.quiet, yes=args.yes)
            successful += len(good); failures += len(bad); skipped += len(plan.skipped) + plan.runtime_skipped
            completed_categories.update((plan.folder, operation.category) for operation in good)
        created_categories = sum(
            1
            for folder, category in categories_absent_before & completed_categories
            if (folder / category).is_dir()
        )
        elapsed = time.perf_counter() - started
        history_failures = sum(plan.history_error is not None for plan in plans)
        if args.json:
            emit({
                "plans": plan_result,
                "status": "completed" if not failures and not history_failures else "partial_failure",
                "moved": successful,
                "skipped": skipped,
                "failed": failures,
                "history_errors": [plan.history_error for plan in plans if plan.history_error],
                "elapsed_seconds": round(elapsed, 3),
                "destination": [str(plan.folder) for plan in plans],
            }, True)
        elif not args.quiet:
            print(terminal_ui.panel("ORGANIZATION COMPLETE", [
                f"Files considered: {sum(len(plan.operations) + len(plan.skipped) for plan in plans)}",
                f"Files moved: {successful}",
                f"Files skipped: {skipped}",
                f"Failed operations: {failures}",
                f"History write failures: {history_failures}",
                f"Categories created: {created_categories}",
                f"Elapsed: {elapsed:.2f} seconds",
                f"Destination: {', '.join(str(plan.folder) for plan in plans)}",
                "Mode: real execution",
            ]))
        return EXIT_PARTIAL if failures or history_failures else EXIT_OK
    if args.command == "rename":
        plans = [build_rename_plan(folder, scan(folder, config, recursive, hidden), prefix=args.prefix, suffix=args.suffix, start=args.start, digits=args.digits, date_format=args.date_format, date_source=args.date_source) for folder in folders]
        plan_result = [serialize_plan(plan) for plan in plans]
        if args.json:
            if not args.apply or not any(plan.operations for plan in plans):
                emit(plan_result[0] if len(plan_result) == 1 else plan_result, True)
        else:
            for plan in plans:
                if len(plans) > 1: print(f"\nFolder: {plan.folder}")
                print_plan(plan)
        if not args.apply or not any(plan.operations for plan in plans):
            if not args.json:
                print("[INFO] Preview only. Use --apply to rename these files.")
            return EXIT_OK
        if not confirm("Apply all planned renames? [y/N]: ", yes=args.yes, prompt_on_stderr=args.json):
            if args.json:
                emit({"plans": plan_result, "status": "cancelled", "renamed": 0, "failed": 0}, True)
            else:
                print("[INFO] No files were changed.")
            return EXIT_OK
        configure_logging(getattr(args, "verbose", False), config)
        started = time.perf_counter()
        successful, failures = 0, 0
        for plan in plans:
            good, bad = execute_rename_plan(plan, args.quiet)
            successful += len(good); failures += len(bad)
        history_failures = sum(plan.history_error is not None for plan in plans)
        if args.json:
            emit({
                "plans": plan_result,
                "status": "completed" if not failures and not history_failures else "partial_failure",
                "renamed": successful,
                "failed": failures,
                "history_errors": [plan.history_error for plan in plans if plan.history_error],
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }, True)
        elif not args.quiet:
            print(terminal_ui.panel("RENAME COMPLETE", [
                f"Renamed: {successful}",
                f"Failed: {failures}",
                f"History write failures: {history_failures}",
                f"Elapsed: {time.perf_counter() - started:.2f} seconds",
                f"Folder: {', '.join(str(plan.folder) for plan in plans)}",
            ]))
        return EXIT_PARTIAL if failures or history_failures else EXIT_OK
    show_progress = (
        args.command in {"scan", "stats", "find", "largest", "duplicates"}
        and not args.json
        and not args.quiet
        and terminal_ui.supports_color()
    )
    if args.command in {"scan", "stats", "find", "largest", "duplicates"} and not args.json and not args.quiet:
        print(terminal_ui.panel("SCANNING", [
            f"Directory: {', '.join(str(folder) for folder in folders)}",
            f"Depth: {'recursive' if recursive else 'top-level only'}",
            f"Hidden files: {'included' if hidden else 'excluded'}",
            "Collecting regular files; symbolic links and junctions are excluded.",
        ]))
    files: list[Path] = []
    scanned_so_far = 0
    for folder in folders:
        def report_progress(local_count: int, offset: int = scanned_so_far) -> None:
            terminal_ui.progress_count("Scanning", offset + local_count)

        found = scan(
            folder,
            config,
            recursive,
            hidden,
            on_progress=report_progress if show_progress else None,
        )
        files.extend(found)
        scanned_so_far += len(found)
    if show_progress:
        terminal_ui.progress_count("Scanning", scanned_so_far, final=True)
    if args.command == "scan":
        result = [str(p) for p in files]
        if args.json: emit(result, True)
        elif not args.quiet:
            print(terminal_ui.panel("SCAN COMPLETE", [
                f"Directory: {', '.join(str(folder) for folder in folders)}",
                f"Files discovered: {len(files)}",
                f"Depth: {'recursive' if recursive else 'top-level only'}",
                f"Hidden files: {'included' if hidden else 'excluded'}",
            ]))
            print(terminal_ui.table(["FILE"], [[str(path)] for path in files[:50]]))
            if len(files) > 50:
                print(terminal_ui.paint(f"Showing 50 of {len(files)} files.", "muted"))
        return EXIT_OK
    if args.command == "stats":
        result = stats(files, config)
        if args.json: emit(result, True)
        else:
            print(terminal_ui.panel("FOLDER STATISTICS", [
                f"Files found: {result['files_found']}",
                f"Total storage: {human_size(result['total_size'])}",
                f"Categories represented: {len(result['categories'])}",
            ]))
            category_rows = []
            max_count = max(result["categories"].values(), default=0)
            for category, count in sorted(result["categories"].items()):
                bar_width = 16 if max_count else 0
                bar = "#" * (int(count / max_count * bar_width) if max_count else 0)
                category_rows.append([
                    category, str(count), human_size(result["category_sizes"][category]), bar,
                ])
            print(terminal_ui.table(["CATEGORY", "FILES", "STORAGE", "DISTRIBUTION"], category_rows))
        return EXIT_OK
    if args.command == "analyze":
        if args.limit < 0:
            raise ValueError("--limit must be non-negative")
        report = storage_analysis(files, config, args.limit)
        if args.json:
            emit(report, True)
        else:
            rows = [
                f"Files found: {report['files_found']}",
                f"Total size: {human_size(report['total_size'])}",
                "Largest files:",
            ]
            rows.extend(
                f"  {human_size(item['size']):>10}  {item['path']}"
                for item in report["largest_files"]
            )
            rows.append("Oldest files:")
            rows.extend(
                f"  {item['modified']}  {item['path']}"
                for item in report["oldest_files"]
            )
            print(terminal_ui.panel("STORAGE ANALYSIS", rows))
        return EXIT_OK
    if args.command == "largest":
        if not math.isfinite(args.min_size_mb) or args.min_size_mb < 0 or args.limit < 0:
            raise ValueError("--min-size-mb must be non-negative and --limit must be non-negative")
        minimum = int(args.min_size_mb * 1024 * 1024)
        result = []
        for path in files:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size >= minimum:
                result.append({"path": str(path), "size": size})
        result.sort(key=lambda item: item["size"], reverse=True)
        result = result[:max(0, args.limit)]
        if args.json: emit(result, True)
        else:
            print(terminal_ui.panel("LARGEST FILES", [
                f"Matches: {len(result)}",
                f"Threshold: {args.min_size_mb:g} MB",
                f"Directory: {', '.join(str(folder) for folder in folders)}",
            ]))
            print(terminal_ui.table(["SIZE", "PATH"], [[human_size(item["size"]), item["path"]] for item in result]))
        return EXIT_OK
    if args.command == "find":
        if any(value is not None and not math.isfinite(value) for value in (args.min_size_mb, args.max_size_mb)):
            raise ValueError("search size limits must be finite numbers")
        minimum = None if args.min_size_mb is None else int(args.min_size_mb * 1024 * 1024)
        maximum = None if args.max_size_mb is None else int(args.max_size_mb * 1024 * 1024)
        if (minimum is not None and minimum < 0) or (maximum is not None and maximum < 0):
            raise ValueError("search size limits must be non-negative")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("--min-size-mb cannot be greater than --max-size-mb")
        result = search_files(
            files, config, pattern=args.pattern, name=args.name, extension=args.extension,
            category=args.category, min_size=minimum, max_size=maximum,
            modified_after=parse_cli_date(args.modified_after, "--modified-after"),
            modified_before=parse_cli_date(args.modified_before, "--modified-before"),
        )
        if args.json: emit([str(p) for p in result], True)
        else:
            print(terminal_ui.panel("SEARCH RESULTS", [
                f"Directory: {', '.join(str(folder) for folder in folders)}",
                f"Pattern: {args.pattern}",
                f"Matches: {len(result)}",
            ]))
            print(terminal_ui.table(["MATCH"], [[str(path)] for path in result[:50]]))
            if len(result) > 50:
                print(terminal_ui.paint(f"Showing 50 of {len(result)} matches.", "muted"))
        return EXIT_OK
    if args.command == "duplicates":
        if not math.isfinite(args.min_size_mb) or args.min_size_mb < 0:
            raise ValueError("--min-size-mb must be non-negative")
        minimum = int(args.min_size_mb * 1024 * 1024)
        eligible: list[Path] = []
        for path in files:
            try:
                if path.stat().st_size >= minimum:
                    eligible.append(path)
            except OSError as error:
                logging.warning("Cannot inspect %s: %s", path, error)
        result = duplicate_groups(eligible)
        if args.json: emit(result, True)
        elif not result: print(terminal_ui.status("No exact duplicates found.", "info"))
        else:
            print(terminal_ui.panel("DUPLICATE REVIEW", [
                f"Duplicate groups: {len(result)}",
                "No files were deleted. Review paths before taking action.",
                f"Potential duplicate storage: {human_size(sum(group['potential_savings'] for group in result))}",
            ]))
            for group in result:
                print(terminal_ui.panel("MATCHING CONTENT", [
                    f"File size: {human_size(group['size'])}",
                    f"SHA-256: {group['sha256']}",
                    f"Copies: {len(group['files'])}",
                    f"Potential savings: {human_size(group['potential_savings'])}",
                ]))
                print(terminal_ui.table(["PATH"], [[name] for name in group["files"]]))
        return EXIT_OK
    return EXIT_INVALID


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        if not sys.stdin.isatty():
            parser().print_help()
            return EXIT_OK
        return interactive()
    arg_parser = parser()
    args = arg_parser.parse_args(arguments)
    if args.command is None:
        arg_parser.print_help()
        return EXIT_INVALID
    try:
        if getattr(args, "verbose", False) and args.command != "preview":
            logging_config = load_config(getattr(args, "config", None)) if args.command not in {"config", "history", "undo"} else DEFAULT_CONFIG
            configure_logging(True, logging_config)
        return run(args)
    except ValueError as error:
        print(terminal_ui.status(str(error), "failure", sys.stderr), file=sys.stderr)
        return EXIT_INVALID
    except KeyboardInterrupt:
        print("\n[INFO] Operation interrupted.", file=sys.stderr)
        return EXIT_PARTIAL
    except OSError as error:
        logging.getLogger(__name__).debug("Fatal filesystem error", exc_info=True)
        print(terminal_ui.status(f"Filesystem error: {error}", "failure", sys.stderr), file=sys.stderr)
        return EXIT_FATAL
    except EOFError:
        print(terminal_ui.status("Input closed before confirmation; no further action was taken.", "failure", sys.stderr), file=sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
