from __future__ import annotations
import filecmp
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import hashlib
import textwrap
import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path
from collections import defaultdict
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox
from tkinter import ttk
from tkinter import simpledialog
from tkinter import filedialog

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# =========================================================
# CONFIG
# =========================================================

CONFIG_FILE = Path.home() / ".mcodepatcher.json"
APP_ROOT = CONFIG_FILE.parent


DEFAULT_CONFIG = {

    "active_mod": None,
    
    "reference_tracking": True,

    "patch_root": str(

        Path.home() / "MCodePatcher"

    ),

    "active_workspace_versions": {},

    "workspace_versions": {},

    "mods": {}

}

QUIET_TIME_SECONDS = 0.25
MAX_PATCHES_PER_LOOP = 3
MAX_LOG_LINES = 500
MAX_ADVANCED_REFERENCE_HISTORY = 5000
# =========================================================

# REFRESH SCOPES

# =========================================================

REFRESH_MINIMAL = "minimal"

REFRESH_PARTIAL = "partial"

REFRESH_FULL = "full"

REFRESH_PRIORITY = {

    REFRESH_MINIMAL: 0,

    REFRESH_PARTIAL: 1,

    REFRESH_FULL: 2,

}

def load_config():

    APP_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not CONFIG_FILE.exists():

        CONFIG_FILE.write_text(
            json.dumps(DEFAULT_CONFIG, indent=4),
            encoding="utf-8",
        )

    try:

        data = json.loads(
            CONFIG_FILE.read_text(encoding="utf-8")
        )

    except Exception:

        data = dict(DEFAULT_CONFIG)

    data.setdefault("mods", {})
    data.setdefault("patch_root", str(APP_ROOT))
    data.setdefault("active_workspace_versions", {})
    data.setdefault("workspace_versions", {})

    active = data.get("active_mod")

    if (
        not active
        or active == "default"
        or active not in data.get("mods", {})
    ):

        data["active_mod"] = None
        save_config(data)

    return data


def save_config(data):

    CONFIG_FILE.write_text(
        json.dumps(
            data,
            indent=4,
        ),
        encoding="utf-8",
    )


def get_mod_root_path(
    data,
    name,
):

    entry = data.get(
        "mods",
        {},
    ).get(name)

    if isinstance(entry, dict):

        entry = (
            entry.get("path")
            or entry.get("root")
            or entry.get("workspace")
        )

    if not entry:
        return None

    return Path(entry)


def get_workspace_version_entry(
    data,
    name,
    version_key,
):

    if not version_key:
        return None

    versions = data.get(
        "workspace_versions",
        {},
    ).get(
        name,
        {},
    )

    entry = versions.get(version_key)

    if isinstance(entry, str):

        return {
            "path": entry,
        }

    if isinstance(entry, dict):
        return entry

    return None


def get_workspace_version_entry_path(entry):

    if isinstance(entry, str):
        return Path(entry)

    if isinstance(entry, dict) and entry.get("path"):
        return Path(entry["path"])

    return None


def paths_match(
    left,
    right,
):

    try:

        return Path(left).resolve() == Path(right).resolve()

    except Exception:

        return Path(left) == Path(right)


def get_active_workspace_root(
    data,
    name,
):

    active_versions = data.get(
        "active_workspace_versions",
        {},
    )

    version_key = active_versions.get(name)

    entry = get_workspace_version_entry(
        data,
        name,
        version_key,
    )

    if entry and entry.get("path"):
        return Path(entry["path"])

    return get_mod_root_path(
        data,
        name,
    )


def build_mod_map(data):

    results = {}

    for name in data.get(
        "mods",
        {},
    ):

        root = get_active_workspace_root(
            data,
            name,
        )

        if root:
            results[name] = root

    return results


def update_workspace_version_record_for_identity(
    config,
    mod_name,
    workspace_root,
    identity,
):

    versions = config.setdefault(
        "workspace_versions",
        {},
    ).setdefault(
        mod_name,
        {},
    )

    detected_key = identity["generator_folder"]

    record = {
        "path": str(Path(workspace_root)),
        "generator": identity["generator"],
        "mc_version": identity["mc_version"],
        "detected_key": detected_key,
        "label": detected_key,
        "updated": time.time(),
    }

    existing = versions.get(detected_key)

    if isinstance(existing, dict):

        merged = dict(existing)

        if (
            paths_match(
                existing.get("path", ""),
                workspace_root,
            )
            and existing.get("generator") == identity["generator"]
            and existing.get("mc_version") == identity["mc_version"]
            and existing.get("detected_key") == detected_key
        ):

            record["updated"] = existing.get(
                "updated",
                record["updated"],
            )

        merged.update(record)
        record = merged

    versions[detected_key] = record

    config.setdefault(
        "active_workspace_versions",
        {},
    )[mod_name] = detected_key

    return detected_key, record

# =========================================================
# GLOBAL RUNTIME CONFIG
# =========================================================

CONFIG = load_config()

ACTIVE_MOD = CONFIG.get("active_mod")

MODS = build_mod_map(CONFIG)

CUSTOM_PATCH_ROOT = Path(
    CONFIG.get(
        "patch_root",
        str(Path.home() / "MCodePatcher"),
    )
)


WORKSPACE_IDENTITY_RELATIVE_FILES = (
    "build.gradle",
    "mcreator.gradle",
    "gradle.properties",
    "settings.gradle",
    ".mcreator/setupInfo",
)


def build_generator_folder_name(
    generator_name,
    mc_version,
):

    if (
        generator_name == "unknown"
        or mc_version == "UNKNOWN"
    ):

        return "unknown_UNKNOWN"

    return (
        f"{generator_name}_"
        f"{mc_version.replace('.', '')}"
    )


def detect_generator_from_text(content: str):

    lowered = content.lower()

    no_line_comments = re.sub(
        r"(?m)//.*$",
        "",
        lowered,
    )

    no_comments = re.sub(
        r"(?s)/\*.*?\*/",
        "",
        no_line_comments,
    )

    if "id 'net.minecraftforge.gradle'" in lowered:
        return "forge"

    if 'id "net.minecraftforge.gradle"' in lowered:
        return "forge"

    if "net.minecraftforge:forge:" in lowered:
        return "forge"

    if "net.minecraftforge.gradle" in lowered:
        return "forge"

    if "net.minecraftforge" in lowered:
        return "forge"

    if "minecraftforge" in lowered:
        return "forge"

    if "id 'net.neoforged.gradle.userdev'" in lowered:
        return "neoforge"

    if 'id "net.neoforged.gradle.userdev"' in lowered:
        return "neoforge"

    if "net.neoforged:neoforge:" in lowered:
        return "neoforge"

    if "net.neoforged.gradle" in lowered:
        return "neoforge"

    if "net.neoforged" in lowered:
        return "neoforge"

    if "fabric-loom" in lowered:
        return "fabric"

    if "fabricmc" in lowered:
        return "fabric"

    if "org.quiltmc" in lowered:
        return "quilt"

    if "geckolib-neoforge" in lowered:
        return "neoforge"

    if "geckolib-forge" in lowered:
        return "forge"

    if "geckolib-fabric" in lowered:
        return "fabric"

    if "neoforge" in no_comments:
        return "neoforge"

    if "forge" in no_comments:
        return "forge"

    return "unknown"


def detect_generator_from_build_gradle(
    build_gradle: Path,
    log_callback=None,
):

    if not build_gradle.exists():
        return "unknown"

    try:

        return detect_generator_from_text(
            build_gradle.read_text(
                errors="ignore"
            )
        )

    except Exception as e:

        if log_callback:

            log_callback(
                f"Generator detection failed: {e}"
            )

    return "unknown"


def infer_minecraft_version_from_neoforge_version(
    value: str,
):

    match = re.match(
        r"^\s*(\d+)\.(\d+)(?:\.\d+)?",
        value,
    )

    if not match:
        return None

    major = int(match.group(1))
    minor = int(match.group(2))

    if minor == 0:
        return f"1.{major}"

    return f"1.{major}.{minor}"


def detect_minecraft_version_from_text(
    content: str,
):

    patterns = (
        r"mappings\s+channel:\s*['\"]official['\"],\s*version:\s*['\"]([\d\.]+)['\"]",
        r"\bminecraft_version\s*=\s*([\d\.]+)",
        r"['\"]minecraft_version['\"]\s*:\s*['\"]([\d\.]+)['\"]",
        r"net\.minecraftforge:forge:([\d\.]+)-",
        r"geckolib-(?:forge|fabric|neoforge)-([\d\.]+):",
    )

    for pattern in patterns:

        match = re.search(
            pattern,
            content,
            re.IGNORECASE,
        )

        if match:
            return match.group(1)

    match = re.search(
        r"net\.neoforged:neoforge:([\d\.]+)",
        content,
        re.IGNORECASE,
    )

    if match:

        inferred = infer_minecraft_version_from_neoforge_version(
            match.group(1)
        )

        if inferred:
            return inferred

    match = re.search(
        r"\bbuildFileVersion\s*=\s*([\d\.]+)",
        content,
        re.IGNORECASE,
    )

    if match:

        inferred = infer_minecraft_version_from_neoforge_version(
            match.group(1)
        )

        if inferred:
            return inferred

    return "UNKNOWN"


def detect_minecraft_version_from_build_gradle(
    build_gradle: Path,
    log_callback=None,
):

    if not build_gradle.exists():
        return "UNKNOWN"

    try:

        return detect_minecraft_version_from_text(
            build_gradle.read_text(
                errors="ignore"
            )
        )

    except Exception as e:

        if log_callback:

            log_callback(
                f"Version detection failed: {e}"
            )

    return "UNKNOWN"


def detect_generator_from_workspace(
    workspace_root: Path,
    log_callback=None,
):

    for relative in WORKSPACE_IDENTITY_RELATIVE_FILES:

        path = workspace_root / relative

        if not path.exists():
            continue

        try:

            generator = detect_generator_from_text(
                path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                )
            )

        except Exception as e:

            if log_callback:

                log_callback(
                    f"Generator detection failed: {e}"
                )

            continue

        if generator != "unknown":
            return generator

    return "unknown"


def detect_minecraft_version_from_workspace(
    workspace_root: Path,
    log_callback=None,
):

    for relative in WORKSPACE_IDENTITY_RELATIVE_FILES:

        path = workspace_root / relative

        if not path.exists():
            continue

        try:

            mc_version = detect_minecraft_version_from_text(
                path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                )
            )

        except Exception as e:

            if log_callback:

                log_callback(
                    f"Version detection failed: {e}"
                )

            continue

        if mc_version != "UNKNOWN":
            return mc_version

    return "UNKNOWN"


def detect_workspace_identity(
    workspace_root: Path,
    log_callback=None,
):

    generator = detect_generator_from_workspace(
        workspace_root,
        log_callback=log_callback,
    )

    mc_version = detect_minecraft_version_from_workspace(
        workspace_root,
        log_callback=log_callback,
    )

    return {
        "generator": generator,
        "mc_version": mc_version,
        "generator_folder": build_generator_folder_name(
            generator,
            mc_version,
        ),
    }


# =========================================================
# FILTERS
# =========================================================

IGNORED_FILENAMES = {
    "sounds.json",
    "mods.toml",
    "pack.mcmeta",
    "replaceable.json",
    "overworld_carver_replaceables.json",
}

ADVANCED_PATCH_MARKER = (
    "// MPATCHED:ADVANCED"
)

ALLOWED_EXTENSIONS = {
    ".java",
    ".json",
    ".txt",
}

REFERENCE_SNIPPET_PATTERN = re.compile(
    r"^\s*//\s*MSNIPPET(?:_APPLIED)?\s*:\s*([A-Za-z0-9_.\-]+)\s*$",
    re.IGNORECASE,
)

REFERENCE_HEADER_PATTERN = re.compile(
    r"^\s*//\s*MHEADER(?:_APPLIED)?\s*:\s*([A-Za-z0-9_.\-]+)\s*$",
    re.IGNORECASE,
)

# =========================================================
# PATCH CONFIG
# =========================================================

RAW_INJECTION_PATTERN = re.compile(
    r"//\s*msnippet\s*:",
    re.IGNORECASE,
)

APPLIED_INJECTION_PATTERN = re.compile(
    r"//\s*msnippet_applied\s*:",
    re.IGNORECASE,
)

SNIPPET_END_PATTERN = re.compile(
    r"^\s*//\s*MSNIPPET_END\s*:\s*([A-Za-z0-9_.\-]+)\s*$",
    re.IGNORECASE,
)

RAW_HEADER_PATTERN = re.compile(
    r"//\s*mheader\s*:",
    re.IGNORECASE,
)

HEADER_APPLIED_PATTERN = re.compile(
    r"//\s*mheader_applied\s*:",
    re.IGNORECASE,
)

RAW_IMPORT_PATTERN = re.compile(
    r"^[ \t]*//[ \t]*mimport[ \t]*:?[ \t]*(import[ \t]+(?:static[ \t]+)?[^;]+;).*$",
    re.IGNORECASE | re.MULTILINE,
)

APPLIED_IMPORT_PATTERN = re.compile(
    r"^(?P<indent>[ \t]*)(?P<import>import[ \t]+(?:static[ \t]+)?[^;]+;)[ \t]*//[ \t]*MIMPORT_APPLIED[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

PLAIN_IMPORT_PATTERN = re.compile(
    r"^[ \t]*import[ \t]+(?:static[ \t]+)?[^;]+;[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

RAW_REMOVE_START_PATTERN = re.compile(
    r"^[ \t]*//[ \t]*mremove_start[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

RAW_REMOVE_END_PATTERN = re.compile(
    r"^[ \t]*//[ \t]*mremove_end[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

APPLIED_REMOVE_PATTERN = re.compile(
    r"^[ \t]*//[ \t]*mremove_applied[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

INJECTION_FOLDER_NAME = "snippet_injections"
HEADER_FOLDER_NAME = "header_injections"
ADVANCED_INJECTION_FOLDER_NAME = (

    "advanced_snippet_injections"

)

BACKUP_FOLDER_NAME = "file_backups"

BACKUP_INDEX_FILENAME = "backup_index.json"

CURRENT_BACKUP_ROW_ID = "__current_file__"

DISABLED_EXTENSION = ".mpatch_disabled"

SYSTEM_FOLDERS = {
    "snippet_injections",
    "header_injections",
    "advanced_snippet_injections",
    "__pycache__",
}

SYSTEM_FILES = {
    "_PATCH_TEMPLATE.txt",
    "replacements.json",
    "advanced_references.json",
}

PLACEHOLDER_FILENAME = "_PATCH_TEMPLATE.txt"

# =========================================================
# DATA MODELS
# =========================================================


@dataclass
class PendingPatch:
    changed_file: Path
    override_path: Path
    event_type: str


@dataclass
class AppState:
    pending_files: dict[str, PendingPatch] = field(default_factory=dict)
    recently_patched: dict[str, float] = field(default_factory=dict)
    out_of_sync_attempts: dict[str, str] = field(default_factory=dict)
    last_seen_hashes: dict[str, str] = field(
        default_factory=dict
    )

    patch_node_map: dict[str, str] = field(default_factory=dict)
    workspace_node_map: dict[str, str] = field(default_factory=dict)

    recent_activity: list[str] = field(default_factory=list)


# =========================================================
# LOGGER
# =========================================================


class GuiLogger:

    ANSI_ESCAPE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

    def __init__(self):

        self.widget = None
        self.history: list[str] = []

        self.logger = logging.getLogger("MCodePatcher")

        if not self.logger.handlers:

            self.logger.setLevel(logging.INFO)

            handler = logging.StreamHandler(sys.stdout)

            formatter = logging.Formatter("%(message)s")

            handler.setFormatter(formatter)

            self.logger.addHandler(handler)

    def attach_widget(self, widget):

        self.widget = widget

    def log(self, message: str):

        clean = self.ANSI_ESCAPE.sub("", str(message))

        self.logger.info(clean)

        self.history.append(clean)

        if len(self.history) > MAX_LOG_LINES:
            self.history.pop(0)

        if self.widget:

            self.widget.after(0, self._append_gui, clean)

    def _append_gui(self, message):

        try:

            self.widget.configure(state="normal")

            self.widget.insert(tk.END, message + "\n")

            self.widget.see(tk.END)

            self.widget.configure(state="disabled")

        except Exception:
            pass


# =========================================================
# PATCH ENGINE
# =========================================================


class PatchEngine:
    def analyse_workspace_marker_state(
        self,
        path: Path,
    ):
    
        # =====================================
        # ONLY ANALYSE SUPPORTED FILE TYPES
        # =====================================
    
        if path.suffix.lower() not in {
            ".java",
        }:
            return "none"
    
        try:
    
            mtime = path.stat().st_mtime
    
        except Exception:
    
            return "none"
    
        key = (
            str(path),
            self.reference_tracking_enabled,
        )
    
        cached_mtime = (
            self.workspace_marker_mtime_cache.get(key)
        )
    
        if cached_mtime == mtime:
    
            return self.workspace_marker_cache.get(
                key,
                "none",
            )
    
        try:
    
            content = path.read_text(
                encoding="utf-8",
                errors="ignore",
            )
    
        except Exception:
    
            return "none"
    
        found_marker = False
        missing_marker = False
    
        # =====================================
        # SNIPPETS
        # =====================================
    
        for line in content.splitlines():

            marker = self.extract_marker_from_line(
                line,
                "snippet",
                include_applied=True,
            )

            if not marker:
                continue
    
            found_marker = True
    
            injection = self.find_injection_file(
                self.injection_root,
                marker,
            )
    
            if not injection:
    
                missing_marker = True
    
        # =====================================
        # HEADERS
        # =====================================
    
        for line in content.splitlines():

            marker = self.extract_marker_from_line(
                line,
                "header",
                include_applied=True,
            )

            if not marker:
                continue
    
            found_marker = True
    
            header = self.find_injection_file(
                self.header_root,
                marker,
            )
    
            if not header:
    
                missing_marker = True

        # =====================================
        # BUILT-IN SNIPPET-FAMILY MARKERS
        # =====================================

        for line in content.splitlines():

            if self.extract_import_details_from_line(
                line,
                include_applied=True,
            ):

                found_marker = True
                continue

            remove_details = (
                self.extract_remove_details_from_line(
                    line,
                    include_applied=True,
                )
            )

            if remove_details and (
                remove_details["kind"] != "end"
            ):

                found_marker = True
    
        if missing_marker:
            result = "missing"
    
        elif found_marker:
            result = "valid"
    
        else:
            result = "none"
    
        self.workspace_marker_cache[key] = result
        self.workspace_marker_mtime_cache[key] = mtime
    
        return result
    def find_injection_file(
        self,
        root: Path,
        marker_name: str,
    ):
    
        normalized = self.normalize_marker_name(
            marker_name
        )
    
        if not root.exists():
            return None
    
        for file in root.glob("*.txt*"):
    
            # =====================================
            # SKIP DISABLED FILES
            # =====================================
    
            if self.is_disabled(file):
                continue
    
            # =====================================
            # SKIP DISABLED PARENT FOLDERS
            # =====================================
    
            if self.path_is_inside_disabled_folder(
                file
            ):
                continue
    
            candidate = self.normalize_marker_name(
                file.stem
            )
    
            if candidate == normalized:
                return file
    
        return None
    def resolve_optional_disabled_folder(self, path: Path):
        if path.exists():
            return path
    
        disabled = Path(str(path) + DISABLED_EXTENSION)
    
        if disabled.exists():
            return disabled
    
        return path
    def path_is_inside_system_folder(
        self,
        path: Path,
    ):
        for parent in path.parents:
    
            if self.is_special_system_folder(parent):
                return True
    
        return False
    def normalize_marker_name(
        self,
        value: str,
    ):
        return re.sub(
            r"\s+",
            "",
            value,
        ).strip().lower()

    def extract_marker_from_line(
        self,
        line: str,
        kind: str,
        include_applied=True,
    ):
        details = self.extract_marker_details_from_line(
            line,
            kind,
            include_applied=include_applied,
        )

        if not details:
            return None

        return details["marker"]


    def extract_snippet_end_marker_from_line(
        self,
        line: str,
    ):

        match = SNIPPET_END_PATTERN.match(
            line.strip()
        )

        if not match:
            return None

        return self.normalize_marker_name(
            match.group(1)
        )


    def normalize_import_statement(
        self,
        value: str,
    ):

        return re.sub(
            r"\s+",
            " ",
            value,
        ).strip()


    def import_key(
        self,
        value: str,
    ):

        return self.normalize_import_statement(
            value
        )


    def extract_import_details_from_line(
        self,
        line: str,
        include_applied=True,
    ):

        stripped = line.strip()

        raw_match = RAW_IMPORT_PATTERN.match(
            stripped
        )

        if raw_match:

            import_statement = (
                self.normalize_import_statement(
                    raw_match.group(1)
                )
            )

            return {
                "import": import_statement,
                "applied": False,
            }

        if include_applied:

            applied_match = (
                APPLIED_IMPORT_PATTERN.match(line)
            )

            if applied_match:

                import_statement = (
                    self.normalize_import_statement(
                        applied_match.group("import")
                    )
                )

                return {
                    "import": import_statement,
                    "applied": True,
                }

        return None


    def extract_remove_details_from_line(
        self,
        line: str,
        include_applied=True,
    ):

        stripped = line.strip()

        if RAW_REMOVE_START_PATTERN.match(stripped):

            return {
                "kind": "start",
                "applied": False,
            }

        if RAW_REMOVE_END_PATTERN.match(stripped):

            return {
                "kind": "end",
                "applied": False,
            }

        if (
            include_applied
            and APPLIED_REMOVE_PATTERN.match(stripped)
        ):

            return {
                "kind": "applied",
                "applied": True,
            }

        return None


    def extract_marker_details_from_line(
        self,
        line: str,
        kind: str,
        include_applied=True,
    ):
        stripped = line.strip()

        if kind == "snippet":

            marker_patterns = [
                (
                    RAW_INJECTION_PATTERN,
                    r"//\s*msnippet\s*:",
                    False,
                ),
            ]

            if include_applied:

                marker_patterns.append((
                    APPLIED_INJECTION_PATTERN,
                    r"//\s*msnippet_applied\s*:",
                    True,
                ))

        else:

            marker_patterns = [
                (
                    RAW_HEADER_PATTERN,
                    r"//\s*mheader\s*:",
                    False,
                ),
            ]

            if include_applied:

                marker_patterns.append((
                    HEADER_APPLIED_PATTERN,
                    r"//\s*mheader_applied\s*:",
                    True,
                ))

        for pattern, prefix, applied in marker_patterns:

            if not pattern.match(stripped):
                continue

            marker = re.sub(
                prefix,
                "",
                stripped,
                flags=re.IGNORECASE,
            )

            marker = (
                marker
                .split("//")[0]
                .strip()
            )

            marker = self.normalize_marker_name(
                marker
            )

            if not marker:
                return None

            return {
                "marker": marker,
                "applied": applied,
            }

        return None

    def content_has_marker(
        self,
        content: str,
        kind: str,
        marker_name=None,
        include_applied=True,
    ):
        normalized_marker = (
            self.normalize_marker_name(marker_name)
            if marker_name
            else None
        )

        for line in content.splitlines():

            marker = self.extract_marker_from_line(
                line,
                kind,
                include_applied=include_applied,
            )

            if not marker:
                continue

            if (
                normalized_marker is None
                or marker == normalized_marker
            ):
                return True

        return False

    def content_has_any_patch_marker(
        self,
        content: str,
        include_applied=True,
    ):

        if (
            self.content_has_marker(
                content,
                "snippet",
                include_applied=include_applied,
            )
            or self.content_has_marker(
                content,
                "header",
                include_applied=include_applied,
            )
        ):
            return True

        for line in content.splitlines():

            if self.extract_import_details_from_line(
                line,
                include_applied=include_applied,
            ):
                return True

            remove_details = (
                self.extract_remove_details_from_line(
                    line,
                    include_applied=include_applied,
                )
            )

            if remove_details:

                if remove_details["kind"] == "end":
                    continue

                return True

        return False
        
    def normalized_disabled_path(
        self,
        path: Path,
    ):
        text = str(path)
    
        if text.endswith(DISABLED_EXTENSION):
            return Path(
                text[: -len(DISABLED_EXTENSION)]
            )
    
        return path
    
    
    def normalized_name(
        self,
        path: Path,
    ):
        return self.normalized_disabled_path(path).name


    def is_workspace_identity_file(
        self,
        path: Path,
    ):

        try:

            relative = str(
                path.relative_to(
                    self.workspace_root
                )
            ).replace("\\", "/")

        except Exception:

            return False

        return (
            relative in WORKSPACE_IDENTITY_RELATIVE_FILES
            or path.suffix.lower() in {
                ".gradle",
                ".properties",
            }
        )
    
    
    def is_special_system_folder(
        self,
        path: Path,
    ):
        return (
            self.normalized_name(path)
            in SYSTEM_FOLDERS
        )

    def normalize_advanced_target(
        self,
        target,
    ):

        text = str(
            target or ""
        ).replace(
            "\\",
            "/",
        ).strip().strip("\"'")

        if not text:
            return ""

        text = text.rstrip("/")

        for root in (
            getattr(
                self,
                "workspace_root",
                None,
            ),
            getattr(
                self,
                "patch_root",
                None,
            ),
        ):

            if not root:
                continue

            root_text = str(root).replace(
                "\\",
                "/",
            ).rstrip("/")

            if text == root_text:
                return "."

            if text.startswith(root_text + "/"):

                return text[
                    len(root_text) + 1:
                ].strip("/")

        return text.strip("/")


    def get_advanced_scope_targets(
        self,
        scope,
    ):

        if not isinstance(scope, dict):
            scope = {}

        normalized_targets = []

        raw_targets = scope.get(
            "targets",
            [],
        )

        if isinstance(raw_targets, list):

            for entry in raw_targets:

                if isinstance(entry, dict):

                    mode = entry.get(
                        "mode",
                        entry.get(
                            "type",
                            "file",
                        ),
                    )

                    target = entry.get(
                        "target",
                        "",
                    )

                else:

                    mode = scope.get(
                        "mode",
                        "file",
                    )

                    target = entry

                mode = str(
                    mode or "file"
                ).lower()

                if mode == "all":
                    mode = "directory"
                    target = "."

                if mode not in {
                    "directory",
                    "file",
                }:
                    mode = "file"

                target = self.normalize_advanced_target(
                    target
                )

                if not target:
                    continue

                normalized_targets.append({
                    "mode": mode,
                    "target": target,
                })

        if normalized_targets:
            return normalized_targets

        if (
            isinstance(raw_targets, list)
            and raw_targets
        ):
            return []

        mode = str(
            scope.get(
                "mode",
                "file",
            )
            or "file"
        ).lower()

        if mode == "all":
            mode = "directory"
            scope = {
                **scope,
                "target": ".",
            }

        if mode not in {
            "directory",
            "file",
        }:
            mode = "file"

        target = self.normalize_advanced_target(
            scope.get(
                "target",
                "",
            )
        )

        if not target:
            return []

        return [{
            "mode": mode,
            "target": target,
        }]


    def get_advanced_scope_label(
        self,
        scope,
    ):

        targets = self.get_advanced_scope_targets(
            scope
        )

        if len(targets) > 1:
            return f"{len(targets)} targets"

        if not targets:
            return "invalid target"

        target = targets[0]

        return target.get(
            "mode",
            "file",
        )


    def advanced_directory_matches(
        self,
        relative_path: str,
        directory: str,
    ):

        directory = self.normalize_advanced_target(
            directory
        )

        relative_path = self.normalize_advanced_target(
            relative_path
        )

        if directory in {
            "",
            ".",
        }:
            return True

        return (
            relative_path == directory
            or relative_path.startswith(
                directory + "/"
            )
        )

    def scan_advanced_rules(self):
    
        results = []
    
        if not self.advanced_injection_root.exists():
            return results
    
        for file in sorted(
            self.advanced_injection_root.glob(
                "*.inject.json*"
            ),
            key=lambda p: p.name.lower(),
        ):
    
            if not (
                file.name.endswith(".inject.json")
                or file.name.endswith(
                    ".inject.json" + DISABLED_EXTENSION
                )
            ):
                continue
    
            disabled_by_filename = self.is_disabled(file)
    
            try:
    
                raw = json.loads(
                    file.read_text(
                        encoding="utf-8"
                    )
                )
    
                scope = raw.get("scope", {})
                action = raw.get("action", {})
                match = raw.get("match", {})
    
                enabled = (
                    raw.get("enabled", True)
                    and not disabled_by_filename
                )

                targets = (
                    self.get_advanced_scope_targets(
                        scope
                    )
                )
    
                results.append({
                    "path": file,
                    "enabled": enabled,
                    "scope": (
                        self.get_advanced_scope_label(
                            scope
                        )
                    ),
                    "targets": targets,
                    "regex": match.get("regex", False),
                    "type": action.get(
                        "type",
                        "replace_all",
                    ),
                })
    
            except Exception:
    
                results.append({
                    "path": file,
                    "enabled": False,
                    "scope": "INVALID",
                    "regex": False,
                    "type": "INVALID",
                })
    
        return results
    def compute_file_hash(
        self,
        path: Path,
    ):
    
        try:
    
            return hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    
        except Exception:
    
            return None
    def wait_until_file_stable(
        self,
        path: Path,
        timeout=2.0,
        interval=0.05,
    ):
    
        start = time.time()
    
        last_size = -1
        last_mtime = -1
        stable_count = 0
    
        while time.time() - start < timeout:
    
            try:
    
                stat = path.stat()
    
                size = stat.st_size
                mtime = stat.st_mtime
    
            except Exception:
    
                time.sleep(interval)
                continue
    
            if (
                size == last_size
                and mtime == last_mtime
            ):
    
                stable_count += 1
    
                if stable_count >= 3:
                    return True
    
            else:
    
                stable_count = 0
    
            last_size = size
            last_mtime = mtime
    
            time.sleep(interval)
    
        return False
    # =====================================================
    # ADVANCED INJECTION ENGINE
    # =====================================================
    def atomic_write(
        self,
        path: Path,
        content: str,
    ):
    
        temp = path.with_suffix(
            path.suffix + ".tmp"
        )
    
        temp.write_text(
            content,
            encoding="utf-8",
        )
    
        temp.replace(path)

    # =====================================================
    # FILE BACKUPS
    # =====================================================

    def get_backup_relative_path(
        self,
        path: Path,
    ):

        try:

            resolved = path.resolve()
            workspace = self.workspace_root.resolve()

            if resolved.is_relative_to(workspace):

                return str(
                    resolved.relative_to(workspace)
                ).replace("\\", "/")

        except Exception:
            pass

        try:

            return str(
                path.relative_to(self.workspace_root)
            ).replace("\\", "/")

        except Exception:

            return None


    def load_backup_index(self):

        try:

            if not self.backup_index_file.exists():
                return []

            data = json.loads(
                self.backup_index_file.read_text(
                    encoding="utf-8"
                )
            )

            if isinstance(data, list):
                return data

        except Exception as e:

            self.app.log.log(
                f"Backup index load failed: {e}"
            )

        return []


    def save_backup_index(
        self,
        entries,
    ):

        try:

            self.backup_root.mkdir(
                parents=True,
                exist_ok=True,
            )

            self.atomic_write(
                self.backup_index_file,
                json.dumps(
                    entries,
                    indent=4,
                ),
            )

        except Exception as e:

            self.app.log.log(
                f"Backup index save failed: {e}"
            )


    def get_backups_for_file(
        self,
        path: Path,
    ):

        relative = self.get_backup_relative_path(path)

        if not relative:
            return []

        entries = []

        for entry in self.load_backup_index():

            if entry.get("relative_target") != relative:
                continue

            backup_file = Path(
                entry.get("backup_file", "")
            )

            if not backup_file.exists():
                continue

            entries.append(entry)

        entries.sort(
            key=lambda entry: entry.get(
                "timestamp",
                0,
            ),
            reverse=True,
        )

        return entries


    def get_backup_entry(
        self,
        backup_id: str,
    ):

        for entry in self.load_backup_index():

            if entry.get("id") == backup_id:
                return entry

        return None


    def latest_backup_for_file(
        self,
        path: Path,
    ):

        backups = self.get_backups_for_file(path)

        if backups:
            return backups[0]

        return None


    def backup_workspace_file(
        self,
        path: Path,
        reason="pre_patch",
    ):

        path = Path(path)

        if not path.exists() or not path.is_file():
            return None

        relative = self.get_backup_relative_path(path)

        if not relative:
            return None

        content_hash = self.compute_file_hash(path)

        if not content_hash:
            return None

        latest = self.latest_backup_for_file(path)

        if (
            latest
            and latest.get("content_hash") == content_hash
        ):

            return None

        try:

            timestamp = time.time()

            stamp = time.strftime(
                "%Y%m%d-%H%M%S",
                time.localtime(timestamp),
            )

            relative_path = Path(relative)

            suffix = (
                relative_path.suffix
                if relative_path.suffix
                else ".backup"
            )

            backup_name = (
                f"{relative_path.stem}."
                f"{stamp}."
                f"{content_hash[:12]}"
                f"{suffix}"
            )

            backup_dir = (
                self.backup_root /
                "files" /
                relative_path.parent
            )

            backup_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            backup_file = backup_dir / backup_name

            counter = 1

            while backup_file.exists():

                backup_file = (
                    backup_dir /
                    (
                        f"{relative_path.stem}."
                        f"{stamp}."
                        f"{content_hash[:12]}."
                        f"{counter}"
                        f"{suffix}"
                    )
                )

                counter += 1

            shutil.copy2(
                path,
                backup_file,
            )

            relative_hash = hashlib.sha1(
                relative.encode("utf-8")
            ).hexdigest()[:8]

            entry = {

                "id":
                    f"{stamp}-{relative_hash}-{content_hash[:12]}",

                "timestamp":
                    timestamp,

                "reason":
                    reason,

                "target":
                    str(path),

                "relative_target":
                    relative,

                "backup_file":
                    str(backup_file),

                "content_hash":
                    content_hash,

                "size":
                    path.stat().st_size,

                "generator":
                    self.generator_name,

                "mc_version":
                    self.mc_version,
            }

            index = self.load_backup_index()

            index.append(entry)

            index.sort(
                key=lambda item: item.get(
                    "timestamp",
                    0,
                )
            )

            self.save_backup_index(index)

            self.app.log.log(
                f"Backup saved: {relative}"
            )

            return entry

        except Exception as e:

            self.app.log.log(
                f"Backup failed for {path}: {e}"
            )

            return None


    def restore_backup(
        self,
        backup_entry,
    ):

        if not isinstance(backup_entry, dict):
            return None

        relative = backup_entry.get(
            "relative_target",
            "",
        )

        backup_file = Path(
            backup_entry.get(
                "backup_file",
                "",
            )
        )

        if not relative or not backup_file.exists():
            return None

        target = self.workspace_root / Path(relative)

        try:

            backup_resolved = backup_file.resolve()
            backup_root = self.backup_root.resolve()

            if not backup_resolved.is_relative_to(
                backup_root
            ):
                return None

            target_resolved = target.resolve()
            workspace = self.workspace_root.resolve()

            if not target_resolved.is_relative_to(
                workspace
            ):
                return None

        except Exception:

            return None

        try:

            if target.exists() and target.is_file():

                self.backup_workspace_file(
                    target,
                    reason="pre_restore",
                )

            target.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            self.wait_until_file_stable(
                backup_file
            )

            self.app.state.recently_patched[
                str(target)
            ] = time.time()

            shutil.copy2(
                backup_file,
                target,
            )

            with self.app.lock:

                self.app.state.pending_files.pop(
                    str(target),
                    None,
                )

            current_hash = self.compute_file_hash(
                target
            )

            if current_hash:

                self.app.state.last_seen_hashes[
                    str(target)
                ] = current_hash

            cache_key = str(target)

            self.workspace_marker_cache.pop(
                cache_key,
                None,
            )

            self.workspace_marker_mtime_cache.pop(
                cache_key,
                None,
            )

            self.app.log.log(
                f"Backup restored: {relative}"
            )

            return target

        except Exception as e:

            self.app.log.log(
                f"Backup restore failed: {e}"
            )

            return None
        
    def rebuild_advanced_rule_cache(self):
    
        self.advanced_rule_cache = {
    
            "all": [],
            "directory": defaultdict(list),
            "file": defaultdict(list),
        }
    
        if not self.advanced_injection_root.exists():
            return
    
        loaded = 0
    
        for file in self.advanced_injection_root.glob(
            "*.inject.json*"
        ):
        
            if self.is_disabled(file):
                continue
    
            try:
    
                raw = json.loads(
                    file.read_text(
                        encoding="utf-8"
                    )
                )
    
                if not isinstance(raw, dict):
                    continue
    
                if not raw.get("enabled", True):
                    continue
    
                rule = dict(raw)
    
                rule["_source_file"] = file
    
                # =====================================
                # MATCH CONFIG
                # =====================================
    
                match_cfg = rule.get(
                    "match",
                    {}
                )
    
                snippet = match_cfg.get(
                    "snippet"
                )
    
                if not snippet:
                    continue
    
                if isinstance(snippet, list):
    
                    snippet = "\n".join(
                        snippet
                    )
    
                use_regex = match_cfg.get(
                    "regex",
                    False,
                )
    
                flags = 0
    
                regex_flags = match_cfg.get(
                    "flags",
                    []
                )
    
                if "IGNORECASE" in regex_flags:
                    flags |= re.IGNORECASE
    
                if "MULTILINE" in regex_flags:
                    flags |= re.MULTILINE
    
                if "DOTALL" in regex_flags:
                    flags |= re.DOTALL
    
                compiled = None
                
                if use_regex:
                
                    try:
                
                        compiled = re.compile(
                            snippet,
                            flags,
                        )
                
                    except Exception as e:
                
                        self.app.log.log(
                            f"Invalid regex in "
                            f"{file.name}: {e}"
                        )
                
                        continue
    
                rule["_compiled_regex"] = compiled
                rule["_literal_match"] = snippet
                rule["_regex_mode"] = use_regex
    
                # =====================================
                # REPLACEMENT CONFIG
                # =====================================
    
                action = rule.get(
                    "action",
                    {}
                )

                if not isinstance(action, dict):
                    continue
    
                has_inline_replacement = (
                    "replacement" in action
                    and action.get("replacement") is not None
                )

                replacement = (
                    action.get("replacement")
                    if has_inline_replacement
                    else None
                )
    
                if not has_inline_replacement:
    
                    snippet_file = action.get(
                        "replacement_snippet_file"
                    )
    
                    if snippet_file:
    
                        snippet_path = (
                            self.injection_root /
                            snippet_file
                        )
    
                        if snippet_path.exists():
    
                            replacement = (
                                snippet_path.read_text(
                                    encoding="utf-8"
                                )
                            )
    
                if replacement is None:
                    continue
    
                if isinstance(replacement, list):
    
                    replacement = "\n".join(
                        replacement
                    )
    
                rule["_replacement"] = replacement
    
                # =====================================
                # SCOPE INDEXING
                # =====================================
    
                scope = rule.get(
                    "scope",
                    {}
                )

                targets = (
                    self.get_advanced_scope_targets(
                        scope
                    )
                )

                indexed = False

                for target_spec in targets:

                    mode = target_spec.get(
                        "mode",
                        "file",
                    )

                    target = target_spec.get(
                        "target",
                        "",
                    )

                    if mode == "file":

                        self.advanced_rule_cache[
                            "file"
                        ][target].append(rule)

                        indexed = True

                    elif mode == "directory":

                        self.advanced_rule_cache[
                            "directory"
                        ][target].append(rule)

                        indexed = True

                    else:
                        continue

                if not indexed:
                    continue
    
                loaded += 1
    
            except Exception as e:
    
                self.app.log.log(
                    f"Advanced injection load failed: "
                    f"{file.name}: {e}"
                )
    
        self.app.log.log(
            f"Advanced rule cache rebuilt "
            f"({loaded} rules)"
        )
    
    
    def apply_advanced_injections(
        self,
        changed_file: Path,
        content: str,
    ):
    
        try:
    
            relative_path = str(
                changed_file.relative_to(
                    self.workspace_root
                )
            ).replace("\\", "/")
    
        except Exception:
    
            return content, []
    
        rules = self.get_advanced_rules_for_file(
            relative_path
        )
    
        if not rules:
            return content, []

        self.log_patch_scan(
            "Advanced",
            changed_file,
            f"{len(rules)} candidate rule(s)",
        )
    
        updated = content
    
        modified = False
    
        applied_rules = []
        checked_rules = 0
        skipped_rules = 0
        replacement_total = 0
    
        for rule in rules:
    
            if self.should_skip_advanced_rule(
                rule,
                changed_file,
            ):

                skipped_rules += 1
                continue
    
            try:

                checked_rules += 1
    
                action = rule.get(
                    "action",
                    {}
                )
    
                replace_mode = action.get(
                    "type",
                    "replace_all",
                )
    
                replacement = rule[
                    "_replacement"
                ]
    
                # =====================================
                # REGEX MODE
                # =====================================
    
                if rule["_regex_mode"]:
    
                    compiled = rule[
                        "_compiled_regex"
                    ]
    
                    count = 0
    
                    if replace_mode == (
                        "replace_first"
                    ):
    
                        count = 1
    
                    def replacer(match):
    
                        result = replacement
    
                        # =====================================
                        # NUMBERED GROUPS
                        # =====================================
    
                        for i in range(
                            0,
                            len(match.groups()) + 1
                        ):
    
                            try:
    
                                value = match.group(i)
    
                                if value is None:
                                    value = ""
    
                                result = result.replace(
                                    f"${i}",
                                    value,
                                )
    
                            except Exception:
                                pass
    
                        # =====================================
                        # NAMED GROUPS
                        # =====================================
    
                        for key, value in (
                            match.groupdict().items()
                        ):
    
                            if value is None:
                                value = ""
    
                            result = result.replace(
                                f"${{{key}}}",
                                value,
                            )
    
                        return result
    
                    new_updated, replacements_made = compiled.subn(
                        replacer,
                        updated,
                        count=count,
                    )
    
                # =====================================
                # LITERAL MODE
                # =====================================
    
                else:
    
                    literal = rule[
                        "_literal_match"
                    ]
    
                    occurrence_count = updated.count(
                        literal
                    )

                    if occurrence_count <= 0:
                        continue
    
                    if replace_mode == (
                        "replace_first"
                    ):
    
                        replacements_made = 1
    
                        new_updated = updated.replace(
                            literal,
                            replacement,
                            1,
                        )
    
                    else:

                        replacements_made = occurrence_count
    
                        new_updated = updated.replace(
                            literal,
                            replacement,
                        )
    
                # =====================================
                # APPLY RESULT
                # =====================================
    
                if new_updated != updated:
    
                    updated = new_updated
    
                    modified = True
                    replacement_total += replacements_made
    
                    applied_rules.append(rule)
    
                    self.log_patch_applied(
                        "Advanced",
                        changed_file,
                        (
                            f"{rule.get('name', 'Unnamed')} "
                            f"({replacements_made} replacement(s), "
                            f"{replace_mode})"
                        ),
                    )
    
            except Exception as e:
    
                self.app.log.log(
                    f"Advanced injection failed: {e}"
                )
    
        if (
            checked_rules
            and not replacement_total
        ):

            self.app.log.log(
                "Advanced scan result: "
                f"{self.get_log_relative_path(changed_file)} "
                f"(0 replacements, {checked_rules} checked"
                f"{', ' + str(skipped_rules) + ' skipped' if skipped_rules else ''})"
            )

        return updated, applied_rules
    
    def get_advanced_rules_for_file(
        self,
        relative_path: str,
    ):
    
        results = []

        seen = set()

        def add_rules(rules):

            for rule in rules:

                key = str(
                    rule.get(
                        "_source_file",
                        id(rule),
                    )
                )

                if key in seen:
                    continue

                seen.add(key)
                results.append(rule)
    
        add_rules(
            self.advanced_rule_cache["all"]
        )
    
        for directory, rules in (
            self.advanced_rule_cache[
                "directory"
            ].items()
        ):
    
            if self.advanced_directory_matches(
                relative_path,
                directory
            ):
    
                add_rules(rules)
    
        add_rules(
            self.advanced_rule_cache[
                "file"
            ].get(
                relative_path,
                []
            )
        )
    
        return results
    
    def detect_generator(self):

        return detect_generator_from_workspace(
            self.workspace_root,
            log_callback=self.app.log.log,
        )
    
    def path_is_inside_disabled_folder(
        self,
        path: Path,
    ):
    
        for parent in path.parents:
    
            if self.is_disabled(parent):
    
                return True
    
        return False
    
    def __init__(self, app):
    
        self.app = app
        self.workspace_marker_cache = {}
        self.workspace_marker_mtime_cache = {}
        self.workspace_search_cache = []
        self.patch_search_cache = []
        self._replacement_cache = []
        self._replacement_mtime = 0
        self.reference_tracking_enabled = CONFIG.get(
            "reference_tracking",
            True,
        )
        
        self.snippet_reference_index = defaultdict(set)
        self.header_reference_index = defaultdict(set)
        if not ACTIVE_MOD:
            raise RuntimeError("No workspace loaded.")
    
        if ACTIVE_MOD not in MODS:
            raise RuntimeError(
                f"Active workspace '{ACTIVE_MOD}' is not configured."
            )
    
        self.workspace_root = MODS[ACTIVE_MOD]
    
        self.build_gradle = self.workspace_root / "build.gradle"

        identity = detect_workspace_identity(
            self.workspace_root,
            log_callback=self.app.log.log,
        )

        self.generator_name = identity["generator"]

        self.mc_version = identity["mc_version"]

        self.mc_version_folder = self.mc_version.replace(".", "")
    
        self.generator_folder = identity[
            "generator_folder"
        ]

        configured_version = CONFIG.get(
            "active_workspace_versions",
            {},
        ).get(ACTIVE_MOD)

        configured_entry = get_workspace_version_entry(
            CONFIG,
            ACTIVE_MOD,
            configured_version,
        )

        if (
            configured_version
            and configured_entry
            and configured_entry.get("path")
            and configured_version != self.generator_folder
        ):

            try:

                if (
                    paths_match(
                        configured_entry["path"],
                        self.workspace_root,
                    )
                ):

                    config = load_config()

                    detected_entry = get_workspace_version_entry(
                        config,
                        ACTIVE_MOD,
                        self.generator_folder,
                    )

                    detected_entry_path = (
                        get_workspace_version_entry_path(
                            detected_entry
                        )
                    )

                    if (
                        detected_entry_path
                        and not paths_match(
                            detected_entry_path,
                            self.workspace_root,
                        )
                    ):

                        self.app.log.log(
                            "Configured workspace version link is stale: "
                            f"{configured_version} points at this workspace, "
                            f"but {self.generator_folder} is currently "
                            "detected. Using the detected patch tree for "
                            "this session; repoint the detected version "
                            "row in the manager to save this switch."
                        )

                    else:

                        update_workspace_version_record_for_identity(
                            config,
                            ACTIVE_MOD,
                            self.workspace_root,
                            identity,
                        )

                        save_config(config)

                        self.app.refresh_runtime_workspace_config(
                            config
                        )

                        self.app.log.log(
                            "Active workspace version auto-switched: "
                            f"{configured_version} -> "
                            f"{self.generator_folder}"
                        )

            except Exception:
                pass
    
        self.patch_root = (
            CUSTOM_PATCH_ROOT /
            ACTIVE_MOD /
            self.generator_folder
        )

        self.backup_root = (
            CUSTOM_PATCH_ROOT /
            ACTIVE_MOD /
            BACKUP_FOLDER_NAME /
            f"{self.generator_folder}-backups"
        )

        self.backup_index_file = (
            self.backup_root /
            BACKUP_INDEX_FILENAME
        )
    
        self.advanced_injection_root = (
            self.patch_root /
            ADVANCED_INJECTION_FOLDER_NAME
        )
    
        self.injection_root = (
            self.patch_root /
            INJECTION_FOLDER_NAME
        )
    
        self.header_root = (
            self.patch_root /
            HEADER_FOLDER_NAME
        )
    
        self.replacements_file = (
            self.patch_root /
            "replacements.json"
        )
        
        self.advanced_reference_file = (
            self.advanced_injection_root /
            "advanced_references.json"
        )
    
        self.advanced_injection_root = self.resolve_optional_disabled_folder(
            self.advanced_injection_root
        )
    
        self.injection_root = self.resolve_optional_disabled_folder(
            self.injection_root
        )
    
        self.header_root = self.resolve_optional_disabled_folder(
            self.header_root
        )
    
        self.placeholder_content = f"""
        Rename this file to match any generated file from the matching directory.
    
        Then replace its contents with the custom patched version.
    
        - Mod: {ACTIVE_MOD}
        - Generator: {self.generator_name}
        - Minecraft version: {self.mc_version}
        """.strip()
    
        self.reference_relationships = []
        
        self.injection_content_cache = {}
        self.injection_mtime_cache = {}
        
        self.header_content_cache = {}
        self.header_mtime_cache = {}
        

        self.rebuild_advanced_rule_cache()

    # =====================================================
    # VERSION DETECTION
    # =====================================================
    def rebuild_reference_index(self):
    
        self.snippet_reference_index.clear()
        self.header_reference_index.clear()
    
        for path in self.iter_workspace_files():
    
            if path.suffix.lower() != ".java":
                continue
    
            try:
    
                content = path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                )
    
            except Exception:
                continue
    
            for line in content.splitlines():

                marker = self.extract_marker_from_line(
                    line,
                    "snippet",
                    include_applied=True,
                )

                if marker:

                    self.snippet_reference_index[
                        marker
                    ].add(path)

                marker = self.extract_marker_from_line(
                    line,
                    "header",
                    include_applied=True,
                )

                if marker:

                    self.header_reference_index[
                        marker
                    ].add(path)
    def detect_minecraft_version(self):

        return detect_minecraft_version_from_workspace(
            self.workspace_root,
            log_callback=self.app.log.log,
        )
    # =====================================================
    # FILE ITERATION
    # =====================================================

    def iter_workspace_files(self):

        for root, dirs, files in os.walk(
            self.workspace_root
        ):

            root_path = Path(root)

            dirs[:] = [
                d for d in dirs
                if not self.should_ignore_path(
                    root_path / d
                )
            ]

            for file in files:

                path = root_path / file

                if self.should_ignore_path(path):
                    continue

                if path.name.startswith("."):
                    continue

                yield path


    # =====================================================
    # PATCH FILE SCANNING
    # =====================================================

    def scan_patch_files(self):
    
        results = []
    
        for root, dirs, files in os.walk(
            self.patch_root
        ):
    
            root_path = Path(root)
    
            dirs[:] = [
                d for d in dirs
                if (
                    not self.should_ignore_path(root_path / d)
                    and not self.is_special_system_folder(root_path / d)
                    and not self.is_disabled(root_path / d)
                )
            ]
    
            for file in files:
    
                path = root_path / file
                
                normalized_path = (
                    self.normalized_disabled_path(path)
                )
                
                if self.path_is_inside_system_folder(path):
                    continue
                
                if normalized_path.name in SYSTEM_FILES:
                    continue
                
                if normalized_path.name == PLACEHOLDER_FILENAME:
                    continue
                
                if self.should_ignore_path(normalized_path):
                    continue
    
                try:
    
                    relative = self.normalized_disabled_path(
                        path
                    ).relative_to(
                        self.patch_root
                    )
    
                except Exception:
                    continue
    
                workspace_version = (
                    self.to_workspace_path(relative)
                )
    
                exists = workspace_version.exists()
    
                modified = False
    
                if (
                    exists
                    and path.is_file()
                ):
    
                    modified = self.files_differ_for_status(
                        path,
                        workspace_version,
                    )
    
    
                results.append({
                
                    "path": path,
                
                    "exists": exists,
                
                    "disabled": self.is_disabled(path),
                
                    "modified": modified,
                })
    
        return results
    # =====================================================
    # HELPERS
    # =====================================================
    
    def should_ignore_path(
        self,
        path: Path,
    ):
    
        ignored_names = {
            ".git",
            ".gradle",
            ".idea",
            ".DS_Store",
            "build",
            "run",
            "out",
            "bin",
            "__pycache__",
        }
    
        ignored_suffixes = {
            ".class",
            ".log",
            ".tmp",
        }
    
        for parent in [path] + list(path.parents):
    
            if parent.name in ignored_names:
                return True
    
        if path.suffix.lower() in ignored_suffixes:
            return True
    
        return False
    
    
    def get_real_suffix(self, path: Path):
    
        if self.is_disabled(path):
    
            original = Path(
                str(path).replace(
                    DISABLED_EXTENSION,
                    "",
                )
            )
    
            return original.suffix.lower()
    
        return path.suffix.lower()
    
    
    def get_relative_path(
        self,
        path: Path,
    ):
    
        try:
    
            resolved = path.resolve()
    
            workspace = (
                self.workspace_root.resolve()
            )
    
            patch = (
                self.patch_root.resolve()
            )
    
            if resolved.is_relative_to(patch):
    
                relative = resolved.relative_to(
                    patch
                )
    
            elif resolved.is_relative_to(workspace):
    
                relative = resolved.relative_to(
                    workspace
                )
    
            else:
    
                return None
    
            return str(relative).replace(
                "\\",
                "/",
            )
    
        except Exception:
    
            return None
    
    
    def to_workspace_path(
        self,
        path: str | Path,
    ):
    
        path = Path(path)
    
        try:
    
            if path.is_relative_to(
                self.workspace_root
            ):
                return path
    
        except Exception:
            pass
    
        try:
    
            path = path.relative_to(
                self.patch_root
            )
    
        except Exception:
            pass
    
        return (
            self.workspace_root /
            path
        )
    
    
    def to_patch_path(
        self,
        path: str | Path,
    ):
    
        path = Path(path)
    
        try:
    
            if path.is_relative_to(
                self.patch_root
            ):
                return path
    
        except Exception:
            pass
    
        try:
    
            path = path.relative_to(
                self.workspace_root
            )
    
        except Exception:
            pass
    
        return (
            self.patch_root /
            path
        )
    
    def get_patch_counterpart(
        self,
        path: Path,
    ):
        return self.to_patch_path(path)
    
    
    def get_workspace_counterpart(
        self,
        path: Path,
    ):
        return self.to_workspace_path(path)
    
    def is_disabled(self, path: Path):
    
        return str(path).endswith(
            DISABLED_EXTENSION
        )
    
    
    def files_differ(
        self,
        a: Path,
        b: Path,
    ):
    
        if not a.exists() or not b.exists():
            return True
    
        try:

            filecmp.clear_cache()
    
            return not filecmp.cmp(
                a,
                b,
                shallow=False,
            )
    
        except Exception:
    
            return True


    def files_differ_for_status(
        self,
        a: Path,
        b: Path,
    ):

        if not self.files_differ(a, b):
            return False

        suffix = (
            self.get_real_suffix(a)
            or self.get_real_suffix(b)
        )

        if suffix not in ALLOWED_EXTENSIONS:
            return True

        try:

            left = a.read_text(
                encoding="utf-8",
                errors="ignore",
            )

            right = b.read_text(
                encoding="utf-8",
                errors="ignore",
            )

        except Exception:

            return True

        def normalize_text(value: str):

            return (
                value
                .replace("\r\n", "\n")
                .replace("\r", "\n")
                .rstrip("\n")
            )

        return normalize_text(left) != normalize_text(right)
        
    # =====================================================
    # PATCH TREE
    # =====================================================

    def ensure_patch_roots(self):

        self.patch_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        for folder in (
            self.injection_root,
            self.header_root,
            self.advanced_injection_root,
        ):

            if not folder.exists():

                disabled_folder = Path(
                    str(folder) + DISABLED_EXTENSION
                )

                if disabled_folder.exists():
                    continue

            folder.mkdir(
                parents=True,
                exist_ok=True,
            )

        if not self.replacements_file.exists():

            self.replacements_file.write_text(
                "[]",
                encoding="utf-8",
            )

        if not self.advanced_reference_file.exists():

            self.advanced_reference_file.write_text(
                "[]",
                encoding="utf-8",
            )

        self.backup_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        if not self.backup_index_file.exists():

            self.backup_index_file.write_text(
                "[]",
                encoding="utf-8",
            )


    def build_search_cache(
        self,
        root: Path,
    ):

        results = []

        for current_root, dirs, files in os.walk(root):

            root_path = Path(current_root)

            dirs[:] = [
                d for d in dirs
                if not self.should_ignore_path(
                    root_path / d
                )
            ]

            for directory in dirs:

                results.append(root_path / directory)

            for file in files:

                path = root_path / file

                if self.should_ignore_path(path):
                    continue

                if path.name.startswith("."):
                    continue

                results.append(path)

        return results
    
    def sync_patch_tree(self):
    
        self.ensure_patch_roots()
    
        self.app.log.log(
            "Syncing patch tree..."
        )
    
        for root, dirs, _files in os.walk(
            self.workspace_root
        ):
    
            root_path = Path(root)
    
            dirs[:] = [
                d for d in dirs
                if not self.should_ignore_path(
                    root_path / d
                )
            ]
    
            try:
    
                relative = root_path.relative_to(
                    self.workspace_root
                )
    
            except Exception:
                continue
    
            target_dir = (
                self.patch_root /
                relative
            )
    
            target_dir.mkdir(
                parents=True,
                exist_ok=True,
            )
    
            existing = list(
                target_dir.iterdir()
            )
    
            if not existing:
    
                placeholder = (
                    target_dir /
                    PLACEHOLDER_FILENAME
                )
    
                placeholder.write_text(
                    self.placeholder_content,
                    encoding="utf-8",
                )
    
        self.app.log.log(
            "Patch tree synced."
        )

    # =====================================================
    # REPLACEMENTS
    # =====================================================

    def load_replacements(self):
    
        try:
    
            mtime = self.replacements_file.stat().st_mtime
    
            if mtime != self._replacement_mtime:
    
                data = json.loads(
                    self.replacements_file.read_text(
                        encoding="utf-8"
                    )
                )
    
                self._replacement_cache = (
                    data if isinstance(data, list) else []
                )
    
                self._replacement_mtime = mtime
    
            return self._replacement_cache
    
        except Exception as e:
    
            self.app.log.log(
                f"Replacement load failed: {e}"
            )
    
            return []

    def load_advanced_reference_history(self):
    
        try:
    
            data = json.loads(
                self.advanced_reference_file.read_text(
                    encoding="utf-8"
                )
            )
    
            if not isinstance(data, list):
                return []
    
            return data
    
        except Exception:
    
            return []
    
    def should_skip_advanced_rule(
        self,
        rule,
        changed_file: Path,
    ):
        try:
    
            relative_target = str(
                changed_file.relative_to(
                    self.workspace_root
                )
            ).replace("\\", "/")
    
            rule_file = str(
                rule.get("_source_file", "")
            )
    
            current_hash = self.compute_file_hash(
                changed_file
            )
    
            current_rule_hash = self.compute_file_hash(
                Path(rule_file)
            )
    
            history = (
                self.load_advanced_reference_history()
            )
    
            for entry in history:
    
                if (
                    entry.get("rule_file") == rule_file
                    and entry.get("relative_target")
                    == relative_target
                ):
    
                    saved_target_hash = (
                        entry.get("content_hash_after")
                    )
    
                    saved_rule_hash = (
                        entry.get("rule_hash")
                    )
    
                    if (
                        current_hash
                        == saved_target_hash
                        and current_rule_hash
                        == saved_rule_hash
                    ):
    
                        return True
    
            return False
    
        except Exception:
    
            return False
        
    def save_advanced_reference(
        self,
        rule,
        changed_file,
    ):
    
        try:
    
            history = (
                self.load_advanced_reference_history()
            )
    
            relative_target = str(
                changed_file.relative_to(
                    self.workspace_root
                )
            ).replace("\\", "/")
    
            rule_file = str(
                rule.get(
                    "_source_file",
                    "",
                )
            )
    
            rule_name = rule.get(
                "name",
                Path(rule_file).stem,
            )
    
            content_hash_after = self.compute_file_hash(
                changed_file
            )
            
            rule_hash = self.compute_file_hash(
                Path(rule_file)
            )
    
            dedupe = {}
    
            for entry in history:
    
                key = (
                    entry.get("rule_file"),
                    entry.get("relative_target"),
                )
    
                dedupe[key] = entry
    
            new_entry = {
    
                "rule": rule_name,
    
                "rule_file": rule_file,
    
                "target": str(changed_file),
    
                "relative_target": relative_target,
    
                "timestamp": time.time(),
    
                "content_hash_after": content_hash_after,
                
                "rule_hash": rule_hash,
                
                "generator": self.generator_name,
                
                "mc_version": self.mc_version,
            }
    
            dedupe[
                (
                    rule_file,
                    relative_target,
                )
            ] = new_entry
    
            updated = list(
                dedupe.values()
            )
    
            updated.sort(
                key=lambda x: x.get(
                    "timestamp",
                    0,
                )
            )
    
            updated = updated[
                -MAX_ADVANCED_REFERENCE_HISTORY:
            ]
    
            self.advanced_reference_file.write_text(
    
                json.dumps(
                    updated,
                    indent=4,
                ),
    
                encoding="utf-8",
            )
    
        except Exception as e:
    
            self.app.log.log(
                f"Advanced reference save failed: {e}"
            )
            
    # =====================================================
    # HEADERS
    # =====================================================

    def get_cached_injection_content(
        self,
        path: Path,
    ):

        try:

            mtime = path.stat().st_mtime

        except Exception:

            return ""

        key = (
            str(path),
            self.reference_tracking_enabled,
        )

        if (
            self.injection_mtime_cache.get(key)
            == mtime
        ):

            return self.injection_content_cache.get(
                key,
                "",
            )

        try:

            content = path.read_text(
                encoding="utf-8"
            ).rstrip()

        except Exception:

            return ""
        
        if self.reference_tracking_enabled:

            content = re.sub(
                r"//\s*msnippet\s*:",
                "// MSNIPPET_APPLIED:",
                content,
                flags=re.IGNORECASE,
            )
            
            content = re.sub(
                r"//\s*mheader\s*:",
                "// MHEADER_APPLIED:",
                content,
                flags=re.IGNORECASE,
            )

        self.injection_mtime_cache[key] = mtime
        self.injection_content_cache[key] = content

        return content


    def get_cached_header_content(
        self,
        path: Path,
    ):

        try:

            mtime = path.stat().st_mtime

        except Exception:

            return ""

        key = (
            str(path),
            self.reference_tracking_enabled,
        )

        if (
            self.header_mtime_cache.get(key)
            == mtime
        ):

            return self.header_content_cache.get(
                key,
                "",
            )

        try:

            content = path.read_text(
                encoding="utf-8"
            ).rstrip()

        except Exception:

            return ""
        
        if self.reference_tracking_enabled:

            content = re.sub(
                r"//\s*msnippet\s*:",
                "// MSNIPPET_APPLIED:",
                content,
                flags=re.IGNORECASE,
            )
            
            content = re.sub(
                r"//\s*mheader\s*:",
                "// MHEADER_APPLIED:",
                content,
                flags=re.IGNORECASE,
            )

        self.header_mtime_cache[key] = mtime
        self.header_content_cache[key] = content

        return content


    def get_log_relative_path(
        self,
        path: Path,
    ):

        try:

            return str(
                path.relative_to(
                    self.workspace_root
                )
            ).replace("\\", "/")

        except Exception:

            return path.name


    def log_patch_scan(
        self,
        family: str,
        changed_file: Path,
        detail: str,
    ):

        key = (
            str(changed_file),
            family,
        )

        scan_keys = getattr(
            self,
            "_active_patch_scan_logs",
            None,
        )

        if scan_keys is not None:

            if key in scan_keys:
                return

            scan_keys.add(key)

        self.app.log.log(
            f"{family} scan: "
            f"{self.get_log_relative_path(changed_file)} "
            f"({detail})"
        )


    def log_patch_applied(
        self,
        family: str,
        changed_file: Path,
        detail: str,
    ):

        self.app.log.log(
            f"{family} applied: "
            f"{self.get_log_relative_path(changed_file)} "
            f"({detail})"
        )


    def apply_headers(
        self,
        changed_file: Path,
        content: str,
    ):

        lines = content.splitlines()

        marker_index = None
        header_name = None
        
        for i, line in enumerate(lines):
        
            stripped = line.strip()
        
            applied_match = HEADER_APPLIED_PATTERN.match(stripped)
            raw_match = RAW_HEADER_PATTERN.match(stripped)
        
            if not applied_match and not raw_match:
                continue
        
            marker_index = i

            if applied_match:

                marker = re.sub(
                    r"//\s*mheader_applied\s*:",
                    "",
                    stripped,
                    flags=re.IGNORECASE,
                )

            else:

                marker = re.sub(
                    r"//\s*mheader\s*:",
                    "",
                    stripped,
                    flags=re.IGNORECASE,
                )

            marker = (
                marker
                .split("//")[0]
                .strip()
            )

            header_name = self.normalize_marker_name(
                marker
            )
        
            break

        if marker_index is None:
            return content

        if marker_index is None or header_name is None:
            return content

        self.log_patch_scan(
            "Header",
            changed_file,
            f"marker {header_name}",
        )
        
        header_file = self.find_injection_file(
            self.header_root,
            header_name,
        )

        if not header_file or not header_file.exists():

            self.app.log.log(
                f"Missing header injection: {header_name}"
            )

            return content

        header_content = (
            self.get_cached_header_content(
                header_file
            )
        )

        output = []

        remaining = "\n".join(
            lines[marker_index + 1 :]
        )

        if HEADER_APPLIED_PATTERN.match(
            lines[marker_index].strip()
        ):

            stripped_remaining = remaining.lstrip()

            if stripped_remaining.startswith(
                header_content
            ):

                remaining = stripped_remaining[
                    len(header_content):
                ].lstrip("\r\n")

        # =====================================
        # HEADER CONTENT
        # =====================================

        if header_content:

            output.append(header_content)

        # =====================================
        # REFERENCE TRACKING BOUNDARY
        # =====================================

        if self.reference_tracking_enabled:

            if output:
                output.append("")

            output.append(
                f"// MHEADER_APPLIED:{header_name}"
            )

        self.reference_relationships.append({

            "type": "header",

            "source": str(header_file),
            
            "target": str(changed_file),

            "marker": header_name,
        })

        # =====================================
        # REMAINING FILE CONTENT
        # =====================================

        if remaining.strip():

            output.append("")
            output.append(remaining)

        result = "\n".join(output)

        if result != content:

            self.log_patch_applied(
                "Header",
                changed_file,
                f"{header_name} from {header_file.name}",
            )

        return result


    # =====================================================
    # INJECTIONS
    # =====================================================

    def get_line_indent(
        self,
        line: str,
    ):

        return line[
            : len(line) - len(line.lstrip(" \t"))
        ]


    def indent_injected_content(
        self,
        content: str,
        indent: str,
    ):

        if not content or not indent:
            return content

        dedented = textwrap.dedent(content)

        return "\n".join(
            (
                f"{indent}{line}"
                if line.strip()
                else line
            )
            for line in dedented.splitlines()
        )


    def apply_injections(
        self,
        changed_file: Path,
        content: str,
    ):

        lines = content.splitlines()

        output = []

        changed = False

        markers_seen = 0
        applied_count = 0
        refreshed_count = 0
        cleaned_count = 0

        i = 0

        while i < len(lines):

            line = lines[i]

            stripped = line.strip()

            snippet_end = (
                self.extract_snippet_end_marker_from_line(
                    line
                )
            )

            if snippet_end:

                markers_seen += 1

                if self.reference_tracking_enabled:

                    output.append(line)

                else:

                    changed = True
                    cleaned_count += 1

                i += 1
                continue

            # =====================================
            # APPLIED MARKER
            # Refresh bounded blocks when possible.
            # =====================================

            applied_details = (
                self.extract_marker_details_from_line(
                    line,
                    "snippet",
                    include_applied=True,
                )
            )

            if (
                applied_details
                and applied_details["applied"]
            ):

                markers_seen += 1

                injection_name = applied_details[
                    "marker"
                ]

                line_indent = self.get_line_indent(
                    line
                )

                injection_file = (
                    self.find_injection_file(
                        self.injection_root,
                        injection_name,
                    )
                )

                injected = None

                if (
                    injection_file
                    and injection_file.exists()
                ):

                    injected = (
                        self.get_cached_injection_content(
                            injection_file
                        )
                    )

                    injected = (
                        self.indent_injected_content(
                            injected,
                            line_indent,
                        )
                    )

                block_end_index = None

                for candidate in range(
                    i + 1,
                    len(lines),
                ):

                    end_marker = (
                        self.extract_snippet_end_marker_from_line(
                            lines[candidate]
                        )
                    )

                    if end_marker == injection_name:

                        block_end_index = candidate
                        break

                if block_end_index is not None:

                    if injected is not None:

                        if self.reference_tracking_enabled:

                            output.append(
                                f"{line_indent}"
                                f"// MSNIPPET_APPLIED:{injection_name}"
                            )

                            if injected:

                                output.append(injected)

                            output.append(
                                f"{line_indent}"
                                f"// MSNIPPET_END:{injection_name}"
                            )

                        else:

                            if injected:

                                output.append(injected)

                        self.reference_relationships.append({

                            "type": "snippet",

                            "source": str(injection_file),

                            "target": str(changed_file),

                            "marker": injection_name,
                        })

                        changed = True
                        refreshed_count += 1

                    else:

                        if self.reference_tracking_enabled:

                            output.append(line)
                            output.extend(
                                lines[
                                    i + 1:
                                    block_end_index + 1
                                ]
                            )

                        else:

                            output.extend(
                                lines[
                                    i + 1:
                                    block_end_index
                                ]
                            )

                            changed = True
                            cleaned_count += 1

                    i = block_end_index + 1
                    continue

                if injected is not None:

                    injected_lines = (
                        injected.splitlines()
                    )

                    if injected_lines:

                        legacy_block = lines[
                            i + 1:
                            i + 1 + len(injected_lines)
                        ]

                        if legacy_block == injected_lines:

                            if self.reference_tracking_enabled:

                                output.append(
                                    f"{line_indent}"
                                    f"// MSNIPPET_APPLIED:{injection_name}"
                                )

                                output.append(injected)

                                output.append(
                                    f"{line_indent}"
                                    f"// MSNIPPET_END:{injection_name}"
                                )

                            else:

                                output.append(injected)

                            self.reference_relationships.append({

                                "type": "snippet",

                                "source": str(injection_file),

                                "target": str(changed_file),

                                "marker": injection_name,
                            })

                            changed = True
                            refreshed_count += 1

                            i += 1 + len(injected_lines)
                            continue

                if not self.reference_tracking_enabled:

                    changed = True
                    cleaned_count += 1
                    i += 1
                    continue

                output.append(line)
                i += 1
                continue

            # =====================================
            # RAW INJECTION MARKER
            # =====================================

            if RAW_INJECTION_PATTERN.match(
                stripped
            ):

                markers_seen += 1

                line_indent = self.get_line_indent(
                    line
                )

                injection_name = re.sub(
                    r"//\s*msnippet\s*:",
                    "",
                    stripped,
                    flags=re.IGNORECASE,
                )

                injection_name = (
                    injection_name
                    .split("//")[0]
                    .strip()
                )

                injection_name = (
                    self.normalize_marker_name(
                        injection_name
                    )
                )

                injection_file = (
                    self.find_injection_file(
                        self.injection_root,
                        injection_name,
                    )
                )

                if (
                    injection_file
                    and injection_file.exists()
                ):

                    injected = (
                        self.get_cached_injection_content(
                            injection_file
                        )
                    )

                    injected = (
                        self.indent_injected_content(
                            injected,
                            line_indent,
                        )
                    )

                    # =================================
                    # REFERENCE TRACKING
                    # =================================

                    if self.reference_tracking_enabled:

                        output.append(
                            f"{line_indent}"
                            f"// MSNIPPET_APPLIED:{injection_name}"
                        )

                    # =================================
                    # INJECT CONTENT
                    # =================================

                    if injected:

                        output.append(injected)

                    if self.reference_tracking_enabled:

                        output.append(
                            f"{line_indent}"
                            f"// MSNIPPET_END:{injection_name}"
                        )

                    changed = True
                    applied_count += 1

                    self.reference_relationships.append({
                    
                        "type": "snippet",
                    
                        "source": str(injection_file),
                    
                        "target": str(changed_file),
                    
                        "marker": injection_name,
                    })

                    i += 1
                    continue

                else:

                    expected = (
                        self.injection_root /
                        f"{injection_name}.txt"
                    )

                    self.app.log.log(
                        "Missing snippet injection: "
                        f"{injection_name} "
                        f"(expected: {expected.name})"
                    )

            # =====================================
            # NORMAL LINE
            # =====================================

            output.append(line)
            i += 1

        if not changed:
            return content

        if markers_seen:

            self.log_patch_scan(
                "Snippet",
                changed_file,
                f"{markers_seen} marker(s)",
            )

        summary = []

        if applied_count:
            summary.append(
                f"{applied_count} inserted"
            )

        if refreshed_count:
            summary.append(
                f"{refreshed_count} refreshed"
            )

        if cleaned_count:
            summary.append(
                f"{cleaned_count} cleaned"
            )

        if summary:

            self.log_patch_applied(
                "Snippet",
                changed_file,
                ", ".join(summary),
            )

        return "\n".join(output)


    def apply_removal_blocks(
        self,
        changed_file: Path,
        content: str,
    ):

        lines = content.splitlines()

        output = []

        changed = False
        markers_seen = 0
        removed_blocks = 0
        cleaned_markers = 0

        i = 0

        while i < len(lines):

            line = lines[i]

            stripped = line.strip()

            if APPLIED_REMOVE_PATTERN.match(stripped):

                markers_seen += 1

                if self.reference_tracking_enabled:

                    output.append(line)

                else:

                    changed = True
                    cleaned_markers += 1

                i += 1
                continue

            if RAW_REMOVE_START_PATTERN.match(stripped):

                markers_seen += 1

                end_index = None

                for candidate in range(
                    i + 1,
                    len(lines),
                ):

                    if RAW_REMOVE_END_PATTERN.match(
                        lines[candidate].strip()
                    ):

                        end_index = candidate
                        break

                if end_index is None:

                    self.app.log.log(
                        "MREMOVE_START without "
                        f"MREMOVE_END in {changed_file.name}"
                    )

                    output.append(line)
                    i += 1
                    continue

                indent = self.get_line_indent(line)

                if self.reference_tracking_enabled:

                    output.append(
                        f"{indent}// MREMOVE_APPLIED"
                    )

                self.reference_relationships.append({

                    "type": "removal",

                    "source": "",

                    "target": str(changed_file),

                    "marker": "mremove",
                })

                changed = True
                removed_blocks += 1
                i = end_index + 1
                continue

            output.append(line)
            i += 1

        if not changed:
            return content

        if markers_seen:

            self.log_patch_scan(
                "Removal",
                changed_file,
                f"{markers_seen} marker(s)",
            )

        summary = []

        if removed_blocks:
            summary.append(
                f"{removed_blocks} block(s) removed"
            )

        if cleaned_markers:
            summary.append(
                f"{cleaned_markers} marker(s) cleaned"
            )

        if summary:

            self.log_patch_applied(
                "Removal",
                changed_file,
                ", ".join(summary),
            )

        return "\n".join(output)


    def format_import_line(
        self,
        import_statement: str,
    ):

        if self.reference_tracking_enabled:

            return (
                f"{import_statement} "
                f"// MIMPORT_APPLIED"
            )

        return import_statement


    def find_import_insert_index(
        self,
        lines: list[str],
    ):

        last_import_index = None

        package_index = None

        for index, line in enumerate(lines):

            stripped = line.strip()

            if stripped.startswith("package "):

                package_index = index

            if (
                PLAIN_IMPORT_PATTERN.match(stripped)
                or APPLIED_IMPORT_PATTERN.match(line)
            ):

                last_import_index = index

        if last_import_index is not None:

            return last_import_index + 1

        if package_index is None:

            return 0

        insert_index = package_index + 1

        if (
            insert_index < len(lines)
            and not lines[insert_index].strip()
        ):

            insert_index += 1

        return insert_index


    def apply_inline_imports(
        self,
        changed_file: Path,
        content: str,
    ):

        lines = content.splitlines()

        output = []

        collected_imports = []

        existing_imports = {}

        changed = False
        raw_markers = 0
        applied_markers = 0
        inserted_count = 0
        annotated_count = 0
        cleaned_count = 0

        def log_import_activity():

            marker_count = (
                raw_markers + applied_markers
            )

            if marker_count:

                self.log_patch_scan(
                    "Import",
                    changed_file,
                    f"{marker_count} marker(s)",
                )

            summary = []

            if inserted_count:
                summary.append(
                    f"{inserted_count} inserted"
                )

            if annotated_count:
                summary.append(
                    f"{annotated_count} marked existing"
                )

            if cleaned_count:
                summary.append(
                    f"{cleaned_count} cleaned"
                )

            if summary:

                self.log_patch_applied(
                    "Import",
                    changed_file,
                    ", ".join(summary),
                )

        for line in lines:

            import_details = (
                self.extract_import_details_from_line(
                    line,
                    include_applied=False,
                )
            )

            if import_details:

                raw_markers += 1

                import_statement = (
                    import_details["import"]
                )

                key = self.import_key(
                    import_statement
                )

                if key not in [
                    item[0]
                    for item in collected_imports
                ]:

                    collected_imports.append(
                        (
                            key,
                            import_statement,
                        )
                    )

                self.reference_relationships.append({

                    "type": "import",

                    "source": "",

                    "target": str(changed_file),

                    "marker": import_statement,
                })

                changed = True
                continue

            applied_match = (
                APPLIED_IMPORT_PATTERN.match(line)
            )

            if applied_match:

                applied_markers += 1

                import_statement = (
                    self.normalize_import_statement(
                        applied_match.group("import")
                    )
                )

                key = self.import_key(
                    import_statement
                )

                existing_imports[key] = len(output)

                if self.reference_tracking_enabled:

                    output.append(
                        self.format_import_line(
                            import_statement
                        )
                    )

                else:

                    output.append(import_statement)
                    changed = True
                    cleaned_count += 1

                continue

            stripped = line.strip()

            if PLAIN_IMPORT_PATTERN.match(stripped):

                import_statement = (
                    self.normalize_import_statement(
                        stripped
                    )
                )

                key = self.import_key(
                    import_statement
                )

                existing_imports[key] = len(output)

            output.append(line)

        if not collected_imports:

            if not changed:
                return content

            log_import_activity()

            return "\n".join(output)

        insert_lines = []

        existing_keys = set(existing_imports.keys())

        for key, import_statement in collected_imports:

            if key in existing_keys:

                if self.reference_tracking_enabled:

                    index = existing_imports[key]

                    current = output[index]

                    if (
                        APPLIED_IMPORT_PATTERN.match(current)
                        or "//" in current
                    ):

                        continue

                    output[index] = self.format_import_line(
                        self.normalize_import_statement(
                            current.strip()
                        )
                    )

                    changed = True
                    annotated_count += 1

                continue

            insert_lines.append(
                self.format_import_line(
                    import_statement
                )
            )

            existing_keys.add(key)
            changed = True
            inserted_count += 1

        if insert_lines:

            insert_index = self.find_import_insert_index(
                output
            )

            needs_blank_after = (
                insert_index >= len(output)
                or bool(output[insert_index].strip())
            )

            output[insert_index:insert_index] = (
                insert_lines
            )

            if needs_blank_after:

                output.insert(
                    insert_index + len(insert_lines),
                    "",
                )

        if not changed:
            return content

        log_import_activity()

        return "\n".join(output)


    # =====================================================
    # REPLACEMENTS
    # =====================================================

    def apply_replacements(
        self,
        changed_file: Path,
        content: str,
    ):

        replacements = self.load_replacements()

        try:

            relative_path = str(
                changed_file.relative_to(
                    self.workspace_root
                )
            ).replace("\\", "/")

        except Exception:

            return content

        updated = content

        candidate_count = 0

        for patch in replacements:

            if not isinstance(patch, dict):
                continue

            if not patch.get("enabled", True):
                continue

            if patch.get("file") != relative_path:
                continue

            if not isinstance(patch.get("find"), str):
                continue

            if not isinstance(patch.get("replace"), str):
                continue

            candidate_count += 1

        if candidate_count:

            self.log_patch_scan(
                "Replacement",
                changed_file,
                f"{candidate_count} candidate patch(es)",
            )

        for patch in replacements:

            try:

                if not isinstance(patch, dict):
                    continue

                if not patch.get("enabled", True):
                    continue

                if patch.get("file") != relative_path:
                    continue

                find = patch.get("find")
                replace = patch.get("replace")

                if not isinstance(find, str):
                    continue

                if not isinstance(replace, str):
                    continue

                occurrence_count = updated.count(find)

                if occurrence_count <= 0:

                    self.app.log.log(
                        f"Replacement not found: "
                        f"{patch.get('name', 'Unnamed')}"
                    )

                    continue

                # =================================
                # REFERENCE TRACKING CONVERSION
                # =================================

                if self.reference_tracking_enabled:

                    replace = re.sub(
                        r"//\s*msnippet(?:_applied)?\s*:\s*([^\n\r]+)",
                        r"// MSNIPPET_APPLIED:\1",
                        replace,
                        flags=re.IGNORECASE,
                    )

                    replace = re.sub(
                        r"//\s*mheader(?:_applied)?\s*:\s*([^\n\r]+)",
                        r"// MHEADER_APPLIED:\1",
                        replace,
                        flags=re.IGNORECASE,
                    )

                # =================================
                # APPLY REPLACEMENT
                # =================================

                if patch.get("replace_mode") == "first":
    
                    replacements_made = 1

                    updated = updated.replace(
                        find,
                        replace,
                        1,
                    )

                else:
    
                    replacements_made = occurrence_count

                    updated = updated.replace(
                        find,
                        replace,
                    )

                self.log_patch_applied(
                    "Replacement",
                    changed_file,
                    (
                        f"{patch.get('name', 'Unnamed')} "
                        f"({replacements_made} replacement(s))"
                    ),
                )

            except Exception as e:

                self.app.log.log(
                    f"Replacement failed: {e}"
                )

        return updated


    # =====================================================
    # ARBITRARY FILES
    # =====================================================

    def apply_arbitrary_files(self):
        if not self.app.patching_enabled.get():
            return 0
        copied = 0

        for root, dirs, files in os.walk(
            self.patch_root
        ):

            root_path = Path(root)

            dirs[:] = [
                d for d in dirs
                if (
                    not self.should_ignore_path(root_path / d)
                    and not self.is_disabled(root_path / d)
                    and not self.is_special_system_folder(root_path / d)
                )
            ]

            for file in files:

                source_file = root_path / file

                if self.path_is_inside_system_folder(source_file):
                    continue

                if file in SYSTEM_FILES:
                    continue

                if file == PLACEHOLDER_FILENAME:
                    continue

                if self.should_ignore_path(
                    source_file
                ):
                    continue

                if self.is_disabled(source_file):
                    continue

                if self.path_is_inside_disabled_folder(
                    source_file
                ):
                    continue

                try:

                    relative = source_file.relative_to(
                        self.patch_root
                    )

                except Exception:
                    continue

                target_file = (
                    self.to_workspace_path(relative)
                )

                target_file.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                if self.files_differ(
                    source_file,
                    target_file,
                ):

                    self.backup_workspace_file(
                        target_file,
                        reason="pre_arbitrary_copy",
                    )

                    shutil.copy2(
                        source_file,
                        target_file,
                    )

                    copied += 1

        return copied


    # =====================================================
    # PIPELINE
    # =====================================================

    def patch_file(
        self,
        changed_file: Path,
        override_path: Path,
    ):

        if not self.app.patching_enabled.get():
            return
        self.app.log.log(
            f"[PATCH FILE] "
            f"{changed_file} <- {override_path}"
        )

        previous_scan_logs = getattr(
            self,
            "_active_patch_scan_logs",
            None,
        )

        self._active_patch_scan_logs = set()

        try:

            target_missing = not changed_file.exists()

            has_override = (
                override_path.exists()
                and not self.is_disabled(
                    override_path
                )
            )

            if target_missing and not has_override:
                return

            if not target_missing:

                self.wait_until_file_stable(
                    changed_file
                )

                original = changed_file.read_text(
                    encoding="utf-8",
                    errors="ignore",
                )

            else:

                original = ""

            content = original
            
            self.reference_relationships = [
                r for r in self.reference_relationships
                if r.get("target") != str(changed_file)
            ]

            # =====================================
            # FILE OVERRIDE
            # =====================================

            if has_override:

                self.wait_until_file_stable(
                    override_path
                )

                content = override_path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                )

            # =====================================
            # PATCH PIPELINE
            # =====================================

            content = self.apply_headers(
                changed_file,
                content,
            )
            
            for _ in range(5):

                updated_content = self.apply_injections(
                    changed_file,
                    content,
                )

                if updated_content == content:
                    break

                content = updated_content

            content = self.apply_removal_blocks(
                changed_file,
                content,
            )

            content = self.apply_inline_imports(
                changed_file,
                content,
            )

            content = self.apply_replacements(
                changed_file,
                content,
            )

            content = self.apply_removal_blocks(
                changed_file,
                content,
            )

            content = self.apply_inline_imports(
                changed_file,
                content,
            )
            
            ADVANCED_PATCH_MARKER = (
                "// MPATCHED:ADVANCED"
            )
            
            content, applied_advanced_rules = (
                self.apply_advanced_injections(
                    changed_file,
                    content,
                )
            )

            content = self.apply_removal_blocks(
                changed_file,
                content,
            )

            content = self.apply_inline_imports(
                changed_file,
                content,
            )

            # =====================================
            # WRITE IF CHANGED
            # =====================================

            if content != original or target_missing:

                self.backup_workspace_file(
                    changed_file,
                    reason="pre_patch",
                )

                self.app.state.recently_patched[
                    str(changed_file)
                ] = time.time()

                changed_file.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                self.atomic_write(
                    changed_file,
                    content,
                )

                cache_key = str(changed_file)

                self.workspace_marker_cache.pop(
                    cache_key,
                    None,
                )

                self.workspace_marker_mtime_cache.pop(
                    cache_key,
                    None,
                )

                for rule in applied_advanced_rules:
	                
                    self.save_advanced_reference(
                        rule,
                        changed_file,
                    )

                self.app.log.log(
                    "Patch write complete: "
                    f"{self.get_log_relative_path(changed_file)}"
                )

        except Exception as e:

            self.app.log.log(
                f"Patch failed: {e}"
            )

        finally:

            self._active_patch_scan_logs = (
                previous_scan_logs
            )


# =========================================================
# WATCHER
# =========================================================


class WorkspaceWatcher(FileSystemEventHandler):

    def __init__(self, app):

        self.app = app

    def process(self, event, event_type):
        
        if event.is_directory:
            return
    
        path = Path(
            getattr(
                event,
                "dest_path",
                event.src_path,
            )
            if event_type == "MOVED"
            else event.src_path
        )
    
        if self.app.engine.should_ignore_path(
            path
        ):
            return

        is_identity_file = (
            self.app.engine.is_workspace_identity_file(
                path
            )
        )
    
        if (
            not is_identity_file
            and
            self.app.engine.get_real_suffix(path)
            not in ALLOWED_EXTENSIONS
        ):
            return
    
        if path.name in IGNORED_FILENAMES:
            return
        
        # =====================================
        # HASH DEDUPE
        # =====================================
        
        current_hash = (
            self.app.engine.compute_file_hash(
                path
            )
        )
        
        previous_hash = (
            self.app.state.last_seen_hashes.get(
                str(path)
            )
        )
        
        if (
            current_hash is not None
            and current_hash == previous_hash
        ):
            return
        
        self.app.state.last_seen_hashes[
            str(path)
        ] = current_hash

        if is_identity_file:

            self.app.schedule_workspace_identity_check(
                changed_path=path,
                event_type=event_type,
            )

            self.app.schedule_refresh(
                scope=REFRESH_FULL,
                changed_paths=[path],
            )

            return
        
        current = time.time()
        
        recent_patch_time = (
            self.app.state.recently_patched.get(str(path))
        )
        
        if recent_patch_time:
        
            delta = current - recent_patch_time
        
            if delta < 2:
        
                return
    
        if str(path) in self.app.state.recently_patched:
    
            delta = (
                current -
                self.app.state.recently_patched[
                    str(path)
                ]
            )
    
            if delta < 2:
                return
    
        try:
    
            relative = path.relative_to(
                self.app.engine.workspace_root
            )
    
        except Exception:
            return

        refresh_scope = (
            REFRESH_FULL
            if event_type == "CREATED"
            else REFRESH_PARTIAL
        )

        self.app.schedule_refresh(
            scope=refresh_scope,
            changed_paths=[path],
        )
    
        override_path = (
            self.app.engine.to_patch_path(path)
        )
    
        has_override = override_path.exists()
    
        has_mcode = False
        has_mheader = False
        has_mimport = False
        has_mremove = False
    
        try:
    
            if path.exists():
    
                preview = path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                )
    
                has_mcode = bool(
                    RAW_INJECTION_PATTERN.search(preview)
                    or APPLIED_INJECTION_PATTERN.search(preview)
                )
    
                has_mheader = bool(
                    RAW_HEADER_PATTERN.search(preview)
                    or HEADER_APPLIED_PATTERN.search(preview)
                )

                has_mimport = bool(
                    RAW_IMPORT_PATTERN.search(preview)
                    or APPLIED_IMPORT_PATTERN.search(preview)
                )

                has_mremove = bool(
                    RAW_REMOVE_START_PATTERN.search(preview)
                    or APPLIED_REMOVE_PATTERN.search(preview)
                )
        
        except Exception:
            pass
        has_advanced_rules = bool(
            self.app.engine.get_advanced_rules_for_file(
                str(relative).replace("\\", "/")
            )
        )
        if (
            not has_override
            and not has_mcode
            and not has_mheader
            and not has_mimport
            and not has_mremove
            and not has_advanced_rules
        ):
            return

        with self.app.lock:
    
            self.app.state.pending_files[
                str(path)
            ] = PendingPatch(
                changed_file=path,
                override_path=override_path,
                event_type=event_type,
            )
    
            self.app.last_event_time = time.time()
    
        self.app.activity_queue.put(
            str(path)
        )

    def on_modified(self, event):
        self.process(event, "MODIFIED")

    def on_created(self, event):
        self.process(event, "CREATED")

    def on_deleted(self, event):
        self.process(event, "DELETED")

    def on_moved(self, event):
        self.process(event, "MOVED")

class PatchWatcher(FileSystemEventHandler):

    def __init__(self, app):

        self.app = app

    def find_marker_affected_workspace_files(
        self,
        kind: str,
        marker: str,
    ):

        engine = self.app.engine

        if engine.reference_tracking_enabled:

            index = (
                engine.snippet_reference_index
                if kind == "snippet"
                else engine.header_reference_index
            )

            candidates = set(
                index.get(
                    marker,
                    set(),
                )
            )

            include_applied = True

        else:

            candidates = set(
                engine.iter_workspace_files()
            )

            include_applied = False

        affected = set()

        for workspace_path in candidates:

            if not workspace_path.is_file():
                continue

            if (
                engine.get_real_suffix(workspace_path)
                not in ALLOWED_EXTENSIONS
            ):
                continue

            try:

                preview = workspace_path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                )

            except Exception:
                continue

            if not engine.content_has_marker(
                preview,
                kind,
                marker_name=marker,
                include_applied=include_applied,
            ):
                continue

            affected.add(workspace_path)

        return affected

    def queue_marker_affected_files(
        self,
        path: Path,
        event_type: str,
        kind: str,
        label: str,
    ):

        marker = self.app.engine.normalize_marker_name(
            self.app.engine
            .normalized_disabled_path(path)
            .stem
        )

        affected = (
            self.find_marker_affected_workspace_files(
                kind,
                marker,
            )
        )

        queued = 0

        if not self.app.engine.is_disabled(path):

            for workspace_path in affected:

                override_path = (
                    self.app.engine.to_patch_path(
                        workspace_path
                    )
                )

                with self.app.lock:

                    self.app.state.pending_files[
                        str(workspace_path)
                    ] = PendingPatch(
                        changed_file=workspace_path,
                        override_path=override_path,
                        event_type=event_type,
                    )

                queued += 1

        self.app.last_event_time = time.time()

        self.app.activity_queue.put(
            f"[{label}] {path.name} "
            f"-> queued {queued} files"
        )

        self.app.schedule_refresh(
            scope=REFRESH_PARTIAL,
            changed_paths=(
                [path] + list(affected)
            ),
        )

        return True

    def process(self, event, event_type):

        
        self.app.log.log(
            f"[PATCH WATCHER] {event_type}: {event.src_path}"
        )
        if event.is_directory:
            return

        path = Path(event.src_path)
        

        # =====================================
        # IGNORE FILTERS
        # =====================================

        if self.app.engine.should_ignore_path(path):
            return

        if (
            self.app.engine.is_disabled(path)
            and not self.app.engine.path_is_inside_system_folder(path)
        ):
        
            self.app.log.log(
                f"[PATCH WATCHER] Rejected disabled file: {path}"
            )
        
            return

        is_text_patch_file = (
            self.app.engine.get_real_suffix(path)
            in ALLOWED_EXTENSIONS
        )

        if path.name in IGNORED_FILENAMES:
            return
        
        # =====================================
        # HASH DEDUPE
        # =====================================
        
        current_hash = (
            self.app.engine.compute_file_hash(
                path
            )
        )
        
        previous_hash = (
            self.app.state.last_seen_hashes.get(
                str(path)
            )
        )
        
        if (
            current_hash is not None
            and current_hash == previous_hash
        ):
        
            self.app.log.log(
                f"[PATCH WATCHER] Hash dedupe skipped: {path}"
            )
        
            return
        
        self.app.state.last_seen_hashes[
            str(path)
        ] = current_hash

        # =====================================
        # ADVANCED RULE RELOAD
        # =====================================
        try:
        
            if is_text_patch_file and path.is_relative_to(
                self.app.engine.advanced_injection_root
            ):
            
                self.app.engine.rebuild_advanced_rule_cache()

                self.app.queue_advanced_rule_candidates(
                    rule_path=path,
                    reason="ADVANCED_RULE_CHANGE",
                )
	            
                self.app.schedule_refresh(
                    scope=REFRESH_PARTIAL,
                    changed_paths=[path],
                )
        
        except Exception:
            pass
        # =====================================
        # SNIPPET INJECTION
        # =====================================

        try:

            if is_text_patch_file and path.is_relative_to(
                self.app.engine.injection_root
            ):

                self.queue_marker_affected_files(
                    path,
                    event_type,
                    "snippet",
                    "SNIPPET",
                )

                return

        except Exception:
            pass

        # =====================================
        # HEADER INJECTION
        # =====================================

        try:

            if is_text_patch_file and path.is_relative_to(
                self.app.engine.header_root
            ):

                self.queue_marker_affected_files(
                    path,
                    event_type,
                    "header",
                    "HEADER",
                )

                return

        except Exception:
            pass
        
        # =====================================
        # DIRECT FILE OVERRIDE
        # =====================================
        
        workspace_path = (
            self.app.engine.to_workspace_path(path)
        )
        
        if workspace_path == path:
            return

        if not path.exists() or not path.is_file():
            return

        normalized_path = (
            self.app.engine.normalized_disabled_path(
                path
            )
        )

        if self.app.engine.path_is_inside_system_folder(
            path
        ):
            return

        if normalized_path.name in SYSTEM_FILES:
            return

        if normalized_path.name == PLACEHOLDER_FILENAME:
            return
        
        if not is_text_patch_file:

            try:

                workspace_path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                self.app.engine.backup_workspace_file(
                    workspace_path,
                    reason="pre_patch_copy",
                )

                shutil.copy2(
                    path,
                    workspace_path,
                )

                self.app.state.recently_patched[
                    str(workspace_path)
                ] = time.time()

                current_target_hash = (
                    self.app.engine.compute_file_hash(
                        workspace_path
                    )
                )

                if current_target_hash:

                    self.app.state.last_seen_hashes[
                        str(workspace_path)
                    ] = current_target_hash

                self.app.log.log(
                    f"[PATCH WATCHER] Copied override: "
                    f"{workspace_path} <- {path}"
                )

                self.app.activity_queue.put(
                    f"[PATCH] {path.name}"
                )

                self.app.schedule_refresh(
                    scope=REFRESH_FULL,
                    changed_paths=[
                        path,
                        workspace_path,
                    ],
                )

            except Exception as e:

                self.app.log.log(
                    f"Patch override copy failed: {e}"
                )

            return

        self.app.log.log(
            f"[PATCH WATCHER] Queueing override: "
            f"{workspace_path} <- {path}"
        )

        with self.app.lock:
        
            self.app.state.pending_files[
                str(workspace_path)
            ] = PendingPatch(
                changed_file=workspace_path,
                override_path=path,
                event_type=event_type,
            )
        
            self.app.last_event_time = time.time()
        
        self.app.activity_queue.put(
            f"[PATCH] {path.name}"
        )

        self.app.schedule_refresh(
            scope=REFRESH_PARTIAL,
            changed_paths=[
                path,
                workspace_path,
            ],
        )
# =========================================================
# TREE MANAGER
# =========================================================


class TreeManager:
    def build_tree_model(
        self,
        root_path,
        glow=False,
        marker_state_cache=None,
    ):
    
        visited = set()
    
        def build_node(
            path,
            is_root=False,
        ):
    
            try:
                resolved = path.resolve()
    
            except Exception:
                return None
    
            if resolved in visited:
                return None
    
            visited.add(resolved)
    
            if not is_root:
    
                if path.name.startswith("."):
                    return None
    
                if self.app.engine.should_ignore_path(path):
                    return None
    
            normalized_name = (
                self.app.engine.normalized_name(path)
            )
    
            real_suffix = (
                self.app.engine.get_real_suffix(path)
            )
    
            tags = []
    
            relative = (
                self.app.engine.get_relative_path(
                    path
                )
            )
    
            # =====================================
            # ACTIVE VERSION
            # =====================================
    
            if (
                path.is_dir()
                and path.parent.name == ACTIVE_MOD
                and path.name
                == self.app.engine.generator_folder
            ):
    
                tags.append("active_version")
    
            # =====================================
            # SPECIAL FOLDERS
            # =====================================
    
            if normalized_name in (
                INJECTION_FOLDER_NAME,
                HEADER_FOLDER_NAME,
            ):
    
                tags.append("injection_folder")
    
            elif (
                normalized_name
                == ADVANCED_INJECTION_FOLDER_NAME
            ):
    
                tags.append(
                    "advanced_injection_folder"
                )
    
            # =====================================
            # DISABLED
            # =====================================
    
            if self.app.engine.is_disabled(path):
    
                tags.append("disabled")
    
            # =====================================
            # MIRROR / MODIFIED STATES
            # =====================================
    
            if relative:
    
                patch_version = (
                    self.app.engine.get_patch_counterpart(
                        path
                    )
                )
    
                # =================================
                # WORKSPACE TREE STATES
                # =================================
    
                if glow:
    
                    marker_state = (
                        marker_state_cache.get(
                            str(path),
                            "none",
                        )
                        if marker_state_cache
                        else "none"
                    )
    
                    if marker_state == "valid":
    
                        tags.append("marker_valid")
    
                    elif marker_state == "missing":
    
                        tags.append("marker_missing")
    
                    elif (
                        patch_version.exists()
                        and patch_version.name
                        != PLACEHOLDER_FILENAME
                        and not self.app.engine
                        .is_disabled(
                            patch_version
                        )
                    ):
    
                        tags.append("mirrored")
    
                        if path.is_file():
    
                            tags.append("modified")
    
                # =================================
                # PATCH TREE STATES
                # =================================
    
                else:
    
                    workspace_version = (
                        self.app.engine
                        .get_workspace_counterpart(
                            path
                        )
                    )
    
                    if workspace_version.exists():
    
                        tags.append("mirrored")
    
            # =====================================
            # DISPLAY NAME
            # =====================================
    
            if normalized_name == INJECTION_FOLDER_NAME:
    
                display = (
                    "⚡ Snippet Injections"
                )
    
            elif normalized_name == HEADER_FOLDER_NAME:
    
                display = (
                    "🗣️ Header Injections"
                )
    
            elif (
                normalized_name
                == ADVANCED_INJECTION_FOLDER_NAME
            ):
    
                display = (
                    "💉 Advanced Injection Rules"
                )
    
            elif path.is_dir():
    
                display = f"📁 {normalized_name}"
    
            elif real_suffix == ".java":
    
                display = f"☕ {path.name}"
    
            else:
    
                display = f"📄 {path.name}"
    
            # =====================================
            # NODE MODEL
            # =====================================
    
            node = {
            
                "type": "normal",
            
                "path": path,
            
                "display": display,
            
                "tags": tags,
            
                "children": [],
            }
    
            # =====================================
            # CHILDREN
            # =====================================
    
            if path.is_dir():
    
                try:
    
                    def sort_key(p: Path):
                    
                        normalized = (
                            self.app.engine.normalized_name(p)
                        )
                    
                        if normalized == INJECTION_FOLDER_NAME:
                            return (0, "")
                    
                        if normalized == HEADER_FOLDER_NAME:
                            return (1, "")
                    
                        if (
                            normalized
                            == ADVANCED_INJECTION_FOLDER_NAME
                        ):
                            return (2, "")
                    
                        if p.is_dir():
                            return (3, normalized.lower())
                    
                        return (4, normalized.lower())
                    
                    children = sorted(
                        path.iterdir(),
                        key=sort_key,
                    )
                    special_names = {
                    
                        INJECTION_FOLDER_NAME,
                    
                        HEADER_FOLDER_NAME,
                    
                        ADVANCED_INJECTION_FOLDER_NAME,
                    }
                    
                    special_seen = False
                    separator_added = False
                    
                    processed_children = []
                    
                    for child in children:
                    
                        normalized = (
                            self.app.engine.normalized_name(
                                child
                            )
                        )
                    
                        is_special = (
                            normalized in special_names
                        )
                    
                        if is_special:
                    
                            special_seen = True
                    
                        elif (
                            special_seen
                            and not separator_added
                            and path == self.app.engine.patch_root
                        ):
                    
                            processed_children.append({
                    
                                "type": "separator",
                    
                                "display":
                                    "──────── File Overrides ────────",
                    
                                "tags": ("separator",),
                            })
                    
                            separator_added = True
                    
                        processed_children.append(child)
                    
                    children = processed_children
    
                except Exception:
    
                    children = []
    
                for child in children:
                
                    # =====================================
                    # SEPARATOR NODE
                    # =====================================
                
                    if isinstance(child, dict):
                
                        node["children"].append(
                            child
                        )
                
                        continue
                
                    # =====================================
                    # NORMAL PATH NODE
                    # =====================================
                
                    child_node = build_node(
                        child,
                        is_root=False,
                    )
                
                    if child_node:
                
                        node["children"].append(
                            child_node
                        )
    
            return node
    
        return build_node(
            root_path,
            is_root=True,
        )
    def populate_empty(
        self,
        tree,
        node_map,
        title,
        message,
    ):
    
        node_map.clear()
    
        tree.delete(*tree.get_children())
    
        root = tree.insert(
            "",
            "end",
            text=title,
            open=True,
            values=["__placeholder__"],
            tags=("placeholder",),
        )
    
        tree.insert(
            root,
            "end",
            text=message,
            values=["__placeholder__"],
            tags=("placeholder",),
        )
        
    def is_separator_item(
        self,
        tree,
        item,
    ):
    
        tags = tree.item(
            item,
            "tags",
        )
    
        return "separator" in tags
    
    def populate_search_results(
        self,
        tree,
        root_path,
        node_map,
        search_text,
    ):
    
        tree.delete(*tree.get_children())
    
        node_map.clear()
    
        search_text = search_text.lower().strip()
    
        if not search_text:
            return
    
        results = []
    
        cache = (
            self.app.engine.workspace_search_cache
            if root_path == self.app.engine.workspace_root
            else self.app.engine.patch_search_cache
        )
        
        for path in cache:
    
            try:
    
                name = path.name.lower()
    
            except Exception:
                continue
    
            if search_text not in name:
                continue
    
            results.append(path)
    
        # =========================================
        # SORT
        # Directories first, then files
        # =========================================
    
        results.sort(
            key=lambda p: (
                p.is_file(),
                p.name.lower(),
            )
        )
    
        for path in results:
    
            relative = self.app.engine.get_relative_path(
                path
            )
    
            if not relative:
                continue
    
            # =====================================
            # ICONS
            # =====================================
    
            if path.is_dir():
    
                icon = "📁"
    
            else:
    
                real_suffix = (
                    self.app.engine.get_real_suffix(path)
                )
    
                if real_suffix == ".java":
    
                    icon = "☕"
    
                elif real_suffix == ".json":
    
                    icon = "📄"
    
                else:
    
                    icon = "📄"
    
            display = (
                f"{icon} {path.name}    "
                f"({relative})"
            )
    
            tags = []
    
            # =====================================
            # PATCH TREE STATES
            # =====================================
    
            if tree == self.app.patch_tree:
    
                if (
                    self.app.engine.is_disabled(path)
                    and not self.app.engine.path_is_inside_system_folder(path)
                ):
                    continue
                
            # =====================================
            # PATCH TREE COLORS
            # =====================================
            
            if tree == self.app.patch_tree:
            
                normalized_name = (
                    self.app.engine.normalized_name(path)
                )
            
                if self.app.engine.is_disabled(path):
            
                    tags.append("disabled")
            
                if normalized_name in (
                    INJECTION_FOLDER_NAME,
                    HEADER_FOLDER_NAME,
                ):
            
                    tags.append("injection_folder")
            
                elif normalized_name == ADVANCED_INJECTION_FOLDER_NAME:
            
                    tags.append("advanced_injection_folder")
    
            # =====================================
            # WORKSPACE TREE STATES
            # =====================================
    
            if tree == self.app.workspace_tree:
            
                marker_state = (
                    self.app.engine.analyse_workspace_marker_state(
                        path
                    )
                )
            
                if marker_state == "valid":
            
                    tags.append("marker_valid")
            
                elif marker_state == "missing":
            
                    tags.append("marker_missing")
            
                else:
            
                    patch_version = (
                        self.app.engine.get_patch_counterpart(path)
                    )
            
                    if (
                        patch_version.exists()
                        and not self.app.engine.is_disabled(
                            patch_version
                        )
                    ):
            
                        tags.append("mirrored")
            
                        if path.is_file():
            
                            tags.append("modified")
    
            node = tree.insert(
                "",
                "end",
                text=display,
                values=[str(path)],
                tags=tags,
            )
    
            node_map[relative] = node

    def __init__(self, app):

        self.app = app

        self.syncing_selection = False
        self.syncing_expand = False

    def populate(
        self,
        tree,
        root_path,
        node_map,
        glow=False,
    ):

        expanded = self.capture_expanded(tree)

        selected = self.capture_selected(tree)

        node_map.clear()

        tree.delete(*tree.get_children())

        root_tags = []
        
        if (
            root_path.name
            == self.app.engine.generator_folder
        ):
            root_tags.append("active_version")
        
        root = tree.insert(
            "",
            "end",
            text=f"📁 {root_path.name}",
            open=True,
            values=[str(root_path)],
            tags=root_tags,
        )

        self.build_recursive(
            tree,
            root,
            root_path,
            node_map,
            expanded,
            glow,
            visited=set(),
        )

        self.restore_selected(
            tree,
            node_map,
            selected,
        )
    
    def build_recursive(
        self,
        tree,
        parent,
        path,
        node_map,
        expanded,
        glow,
        visited,
    ):
    
        try:
            resolved = path.resolve()
        except Exception:
            return
    
        if resolved in visited:
            return
    
        visited.add(resolved)
    
        try:
    
            def sort_key(p: Path):
    
                name = self.app.engine.normalized_name(p)
    
                if name == INJECTION_FOLDER_NAME:
                    return (0, "")
    
                if name == HEADER_FOLDER_NAME:
                    return (1, "")
    
                if name == ADVANCED_INJECTION_FOLDER_NAME:
                    return (2, "")
    
                if p.is_dir():
                    return (3, name.lower())
    
                return (4, name.lower())
    
            children = sorted(
                path.iterdir(),
                key=sort_key,
            )
    
            special_children = []
            normal_children = []
    
            for child in children:
    
                if self.app.engine.normalized_name(child) in (
                    INJECTION_FOLDER_NAME,
                    HEADER_FOLDER_NAME,
                    ADVANCED_INJECTION_FOLDER_NAME,
                ):
    
                    special_children.append(child)
    
                else:
    
                    normal_children.append(child)
    
        except Exception:
            return
    
        rendered_separator = False
    
        for child in special_children + normal_children:
    
            if (
                not rendered_separator
                and child in normal_children
                and path == self.app.engine.patch_root
            ):
    
                tree.insert(
                    parent,
                    "end",
                    text="──────── File Overrides ────────",
                    open=False,
                    values=["__separator__"],
                    tags=("separator",),
                )
    
                rendered_separator = True
    
            if child.name.startswith("."):
                continue
    
            if self.app.engine.should_ignore_path(child):
                continue
    
            normalized_name = (
                self.app.engine.normalized_name(child)
            )
    
            relative = self.app.engine.get_relative_path(
                child
            )
    
            tags = []
    
            if (
                child.is_dir()
                and child.parent.name == ACTIVE_MOD
                and child.name
                == self.app.engine.generator_folder
            ):
    
                tags.append("active_version")
    
            if normalized_name in (
                INJECTION_FOLDER_NAME,
                HEADER_FOLDER_NAME,
            ):
    
                tags.append("injection_folder")
    
            elif normalized_name == ADVANCED_INJECTION_FOLDER_NAME:
    
                tags.append("advanced_injection_folder")
    
            if self.app.engine.is_disabled(child):
    
                tags.append("disabled")
    
            if relative:
    
                patch_version = (
                    self.app.engine.get_patch_counterpart(child)
                )
    
                if (
                    patch_version.exists()
                    and not self.app.engine.is_disabled(
                        patch_version
                    )
                ):
    
                    tags.append("mirrored")
    
                if glow and child.is_file():
                
                    marker_state = (
                        self.app.engine.analyse_workspace_marker_state(
                            child
                        )
                    )
                
                    if marker_state == "valid":
                
                        tags.append("marker_valid")
                
                    elif marker_state == "missing":
                
                        tags.append("marker_missing")
                
                    elif (
                        patch_version.exists()
                        and patch_version.name != PLACEHOLDER_FILENAME
                        and not self.app.engine.is_disabled(
                            patch_version
                        )
                    ):
                
                        tags.append("modified")
    
            display_name = normalized_name
    
            real_suffix = (
                self.app.engine.get_real_suffix(child)
            )
    
            if normalized_name == INJECTION_FOLDER_NAME:
    
                display_name = "⚡ Snippet Injections"
    
            elif normalized_name == HEADER_FOLDER_NAME:
    
                display_name = "🗣️ Header Injections"
    
            elif normalized_name == ADVANCED_INJECTION_FOLDER_NAME:
    
                display_name = "💉 Advanced Injection Rules"
    
            elif child.is_dir():
    
                display_name = f"📁 {normalized_name}"
    
            elif real_suffix == ".java":
    
                display_name = f"☕ {child.name}"
    
            elif real_suffix == ".json":
    
                display_name = f"📄 {child.name}"
    
            node = tree.insert(
                parent,
                "end",
                text=display_name,
                values=[str(child)],
                open=relative in expanded,
                tags=tags,
            )
    
            if relative:
                node_map[relative] = node
    
            if child.is_dir():
    
                if (
                    not self.app.engine.is_disabled(child)
                    or self.app.engine.is_special_system_folder(child)
                ):
    
                    self.build_recursive(
                        tree,
                        node,
                        child,
                        node_map,
                        expanded,
                        glow,
                        visited,
                    )

    # =====================================================
    # STATE
    # =====================================================

    def capture_expanded(self, tree):

        expanded = set()

        def recurse(node):

            if tree.item(node, "open"):

                values = tree.item(node, "values")

                if values:

                    relative = (
                        self.app.engine.get_relative_path(
                            Path(values[0])
                        )
                    )

                    if relative:
                        expanded.add(relative)

            for child in tree.get_children(node):

                recurse(child)

        for root in tree.get_children():

            recurse(root)

        return expanded

    def capture_selected(self, tree):

        selection = tree.selection()

        if not selection:
            return None

        values = tree.item(
            selection[0],
            "values",
        )

        if not values:
            return None

        return values[0]

    def restore_selected(
        self,
        tree,
        node_map,
        selected_path,
    ):

        if not selected_path:
            return

        for node in node_map.values():

            values = tree.item(
                node,
                "values",
            )

            if values and values[0] == selected_path:

                if tree.exists(node):

                    self.app.suppress_selection_side_effects_temporarily()
                
                    tree.selection_set(node)
                    tree.see(node)

                break

    # =====================================================
    # SELECTION
    # =====================================================

    def on_select(self, event):
        
        if self.app.is_refreshing_trees:
            return
        if self.app.selection_side_effects_suppressed():
            return
        if self.syncing_selection:
            return
        if not self.app.engine:
            return

        tree = event.widget

        selection = tree.selection()

        if not selection:
            return

        item = selection[0]
        
        if self.is_separator_item(
            tree,
            item,
        ):
        
            tree.selection_remove(item)
        
            return

        values = tree.item(item, "values")

        if not values:
            return

        selected_path = Path(values[0])
        self.app.update_toggle_button(
            selected_path
        )

        self.app.update_status(selected_path)

        if self.app.syncing_reference_selection:
            return
        
        # =====================================
        # PATCH TREE -> REFERENCE TRACKER
        # =====================================
        
        if (
            tree == self.app.patch_tree
            and not self.app.syncing_reference_selection
        ):
        
            self.app.sync_patch_selection_to_reference_tracker(
                selected_path
            )
        
        # =====================================
        # WORKSPACE TREE -> REFERENCE TRACKER
        # =====================================
        
        if (
            tree == self.app.workspace_tree
            and not self.app.syncing_reference_selection
        ):
        
            self.app.sync_workspace_selection_to_reference_tracker(
                selected_path
            )

        relative = self.app.engine.get_relative_path(
            selected_path
        )

        if not relative:
            return

        if tree == self.app.workspace_tree:

            other_tree = self.app.patch_tree

            other_map = (
                self.app.state.patch_node_map
            )

        else:

            other_tree = self.app.workspace_tree

            other_map = (
                self.app.state.workspace_node_map
            )

        other_node = other_map.get(relative)

        if not other_node:
            return

        self.syncing_selection = True

        try:

            if other_tree.exists(other_node):
            
                other_tree.selection_set(other_node)
                other_tree.see(other_node)


        finally:

            other_tree.after(
                10,
                self.clear_selection_sync,
            )

    def clear_selection_sync(self):

        self.syncing_selection = False

    # =====================================================
    # EXPANSION
    # =====================================================

    
    def sync_expand(self, tree, should_open):
        
        if self.app.is_refreshing_trees:
            return
        if self.syncing_expand:
            return
        if not self.app.engine:
            return
        
        selection = tree.selection()

        if not selection:
            return

        node = selection[0]
        
        if self.is_separator_item(
            tree,
            node,
        ):
        
            return

        values = tree.item(node, "values")

        if not values:
            return

        relative = self.app.engine.get_relative_path(
            Path(values[0])
        )

        if not relative:
            return

        if tree == self.app.workspace_tree:

            other_tree = self.app.patch_tree

            other_map = (
                self.app.state.patch_node_map
            )

        else:

            other_tree = self.app.workspace_tree

            other_map = (
                self.app.state.workspace_node_map
            )

        other_node = other_map.get(relative)

        if not other_node:
            return

        self.syncing_expand = True

        try:

            if other_tree.exists(other_node):
            
                other_tree.item(
                    other_node,
                    open=should_open,
                )

        finally:

            other_tree.after(
                10,
                self.clear_expand_sync,
            )

    def clear_expand_sync(self):

        self.syncing_expand = False


# =========================================================
# MAIN APP
# =========================================================


class MCodePatcherApp:
    def sync_workspace_selection_to_reference_tracker(
        self,
        selected_path: Path,
    ):
    
        if not self.engine:
            return
    
        # =====================================
        # IGNORE DIRECTORIES
        # =====================================
    
        if not selected_path.is_file():
            return
    
        target_relative = (
            self.engine.get_relative_path(
                self.engine.normalized_disabled_path(
                    selected_path
                )
            )
        )
    
        if not target_relative:
            return
    
        # =====================================
        # SEARCH SNIPPET REFERENCES
        # =====================================
    
        snippet_matches = []
    
        for node in self.snippet_tree.get_children():
    
            for child in self.snippet_tree.get_children(node):
    
                values = self.snippet_tree.item(
                    child,
                    "values",
                )
    
                if len(values) < 4:
                    continue
    
                target_path = values[3]
    
                if not target_path:
                    continue
    
                try:
    
                    relative = (
                        self.engine.get_relative_path(
                            self.engine.normalized_disabled_path(
                                Path(target_path)
                            )
                        )
                    )
    
                except Exception:
                    continue
    
                if relative == target_relative:
    
                    snippet_matches.append(child)
    
        if snippet_matches:
    
            self.reference_tabs.select(
                self.snippet_tab
            )
    
            node = snippet_matches[0]
    
            self.expand_to_node(
                self.snippet_tree,
                node,
            )
    
            self.snippet_tree.selection_set(node)
            self.snippet_tree.focus(node)
            self.snippet_tree.see(node)
    
            return
    
        # =====================================
        # SEARCH HEADER REFERENCES
        # =====================================
    
        header_matches = []
    
        for node in self.header_reference_tree.get_children():
    
            for child in self.header_reference_tree.get_children(node):
    
                values = self.header_reference_tree.item(
                    child,
                    "values",
                )
    
                if len(values) < 4:
                    continue
    
                target_path = values[3]
    
                if not target_path:
                    continue
    
                try:
    
                    relative = (
                        self.engine.get_relative_path(
                            self.engine.normalized_disabled_path(
                                Path(target_path)
                            )
                        )
                    )
    
                except Exception:
                    continue
    
                if relative == target_relative:
    
                    header_matches.append(child)
    
        if header_matches:
    
            self.reference_tabs.select(
                self.header_reference_tab
            )
    
            node = header_matches[0]
    
            self.expand_to_node(
                self.header_reference_tree,
                node,
            )
    
            self.header_reference_tree.selection_set(node)
            self.header_reference_tree.focus(node)
            self.header_reference_tree.see(node)
    
            return
        
        # =====================================
        # PATCH OVERRIDE MATCH
        # ONLY FOR REAL ACTIVE FILE OVERRIDES
        # =====================================
        
        if selected_path.is_file():
        
            patch_version = (
                self.engine.get_patch_counterpart(
                    selected_path
                )
            )
        
            has_active_override = (
                patch_version.exists()
                and patch_version.is_file()
                and patch_version.name != PLACEHOLDER_FILENAME
                and not self.engine.is_disabled(
                    patch_version
                )
            )
        
            if has_active_override:
        
                self.reference_tabs.select(
                    self.files_tab
                )
        
                self.select_override_in_reference_tree(
                    patch_version
                )
        
                return
    
    def sync_patch_selection_to_reference_tracker(
        self,
        selected_path: Path,
    ):
        if not self.engine:
            return
    
        normalized = (
            self.engine.get_relative_path(
                self.engine.normalized_disabled_path(
                    selected_path
                )
            )
        )
    
        if not normalized:
            return
        # =====================================
        # SPECIAL PATCH FOLDERS
        # =====================================
        
        normalized_name = (
            self.engine.normalized_name(
                selected_path
            )
        )
        
        if normalized_name == INJECTION_FOLDER_NAME:
        
            self.reference_tabs.select(
                self.snippet_tab
            )
        
            return
        
        if normalized_name == HEADER_FOLDER_NAME:
        
            self.reference_tabs.select(
                self.header_reference_tab
            )
        
            return
        
        if normalized_name == ADVANCED_INJECTION_FOLDER_NAME:
        
            self.reference_tabs.select(
                self.advanced_tab
            )
        
            return
        # =====================================
        # SNIPPET INJECTIONS
        # =====================================
    
        try:
    
            if selected_path.parent == self.engine.injection_root:
    
                self.reference_tabs.select(
                    self.snippet_tab
                )
    
                self.select_path_in_tree(
                    self.snippet_tree,
                    selected_path,
                )
    
                return
    
        except Exception:
            pass
    
        # =====================================
        # HEADER INJECTIONS
        # =====================================
    
        try:
    
            if selected_path.parent == self.engine.header_root:
    
                self.reference_tabs.select(
                    self.header_reference_tab
                )
    
                self.select_path_in_tree(
                    self.header_reference_tree,
                    selected_path,
                )
    
                return
    
        except Exception:
            pass
    
        # =====================================
        # ADVANCED RULES
        # =====================================
        
        try:
        
            normalized_selected = (
                self.engine.normalized_disabled_path(
                    selected_path
                )
            )
        
            normalized_advanced_root = (
                self.engine.normalized_disabled_path(
                    self.engine.advanced_injection_root
                )
            )
        
            if normalized_selected.is_relative_to(
                normalized_advanced_root
            ):
        
                self.reference_tabs.select(
                    self.advanced_tab
                )
        
                self.select_advanced_rule_in_reference_tree(
                    normalized_selected
                )
        
                return
        
        except Exception:
            pass
    
        # =====================================
        # NORMAL PATCH FILES
        # =====================================
        
        self.reference_tabs.select(
            self.files_tab
        )
        
        self.select_override_in_reference_tree(
            selected_path
        )
    def select_advanced_rule_in_reference_tree(
        self,
        selected_path: Path,
    ):
    
        normalized = str(
            self.engine.normalized_disabled_path(
                selected_path
            )
        )
    
        for root in self.advanced_tree.get_children():
    
            values = self.advanced_tree.item(
                root,
                "values",
            )
    
            if len(values) >= 4:
    
                root_rule = values[3]
    
                if root_rule:
    
                    try:
    
                        normalized_root = str(
                            self.engine.normalized_disabled_path(
                                Path(root_rule)
                            )
                        )
    
                    except Exception:
    
                        normalized_root = ""
    
                    if normalized_root == normalized:
    
                        self.expand_to_node(
                            self.advanced_tree,
                            root,
                        )
    
                        self.advanced_tree.selection_set(
                            root
                        )
    
                        self.advanced_tree.focus(root)
    
                        self.advanced_tree.see(root)
    
                        return
    
            for child in self.advanced_tree.get_children(root):
    
                values = self.advanced_tree.item(
                    child,
                    "values",
                )
    
                if len(values) < 4:
                    continue
    
                target_path = values[3]
    
                if not target_path:
                    continue
    
                try:
    
                    normalized_target = str(
                        self.engine.normalized_disabled_path(
                            Path(target_path)
                        )
                    )
    
                except Exception:
                    continue
    
                if normalized_target != normalized:
                    continue
    
                self.expand_to_node(
                    self.advanced_tree,
                    child,
                )
    
                self.advanced_tree.selection_set(
                    child
                )
    
                self.advanced_tree.focus(child)
    
                self.advanced_tree.see(child)
    
                return
    def select_override_in_reference_tree(
        self,
        selected_path: Path,
    ):
    
        normalized = str(selected_path)
    
        for root in self.files_tree.get_children():
    
            for child in self.files_tree.get_children(root):
    
                values = self.files_tree.item(
                    child,
                    "values",
                )
    
                if len(values) < 2:
                    continue
    
                entry_path = values[1]
    
                if not entry_path:
                    continue
    
                try:
    
                    normalized_entry = str(
                        Path(entry_path)
                    )
    
                except Exception:
                    continue
    
                if normalized_entry != normalized:
                    continue
    
                self.expand_to_node(
                    self.files_tree,
                    child,
                )
    
                self.files_tree.selection_set(
                    child
                )
    
                self.files_tree.focus(child)
    
                self.files_tree.see(child)
    
                return
                
    def sync_reference_selection(self, event):

        if self.selection_side_effects_suppressed():
            return

        if self.is_refreshing_trees:
            return

        tree = event.widget

        if self.engine:

            self.update_backup_panel_for_reference_selection(
                tree
            )
    
        if self.syncing_reference_selection:
            return
    
        if not self.engine:
            return
    
        selection = tree.selection()
    
        if not selection:
            return
    
        item = selection[0]
    
        values = tree.item(
            item,
            "values",
        )
    
        if not values:
            return
    
        # =====================================
        # FILE OVERRIDES TAB
        # =====================================
    
        if tree == self.files_tree:
            
            
    
            if len(values) < 2:
                return
    
            patch_path = values[1]
    
            if not patch_path:
                return
    
            try:
    
                patch_path = Path(patch_path)
    
                workspace_path = (
                    self.engine.get_workspace_counterpart(
                        patch_path
                    )
                )
    
            except Exception:
                return
    
            self.syncing_reference_selection = True
    
            try:
    
                self.select_path_in_tree(
                    self.patch_tree,
                    patch_path,
                    additive=False,
                )
    
                if workspace_path.exists():
    
                    self.select_path_in_tree(
                        self.workspace_tree,
                        workspace_path,
                        additive=False,
                    )
    
            finally:
    
                self.root.after(
                    10,
                    self.clear_reference_selection_sync,
                )
    
            return
        
        # =====================================
        # ADVANCED RULE TREE
        # =====================================
        
        if tree == self.advanced_tree:
        
            advanced_path = ""

            parent_item = tree.parent(item)
        
            # =====================================
            # ROOT RULE NODE: values[3] = rule file
            # HISTORY CHILD: values[3] = workspace target
            # =====================================
        
            if len(values) >= 4:
        
                advanced_path = values[3]
        
            if not advanced_path:
                return
        
            try:
        
                advanced_path = Path(
                    advanced_path
                )
        
            except Exception:
                return
        
            self.syncing_reference_selection = True
        
            try:

                if parent_item:

                    self.select_path_in_tree(
                        self.workspace_tree,
                        advanced_path,
                        additive=False,
                    )

                else:
        
                    self.select_path_in_tree(
                        self.patch_tree,
                        advanced_path,
                        additive=False,
                    )
        
            finally:
        
                self.root.after(
                    10,
                    self.clear_reference_selection_sync,
                )
        
            return
        
        # =====================================
        # GENERIC SNIPPET / HEADER SYNC
        # =====================================
        
        source_path = (
            values[2]
            if len(values) >= 3
            else ""
        )
        
        target_path = (
            values[3]
            if len(values) >= 4
            else ""
        )
        
        self.syncing_reference_selection = True
        
        try:
        
            if source_path:
        
                self.select_path_in_tree(
                    self.patch_tree,
                    Path(source_path),
                    additive=False,
                )
        
            if target_path:
        
                self.select_path_in_tree(
                    self.workspace_tree,
                    Path(target_path),
                    additive=False,
                )
        
        finally:
        
            self.root.after(
                10,
                self.clear_reference_selection_sync,
            )
    
    def clear_reference_selection_sync(self):
    
        self.syncing_reference_selection = False
    
    def has_workspace(self):
    
        return self.engine is not None
    
    
    def require_workspace(self):
    
        if self.engine:
            return True
    
        messagebox.showinfo(
            "No Workspace Loaded",
            (
                "No workspace is currently loaded.\n\n"
                "Use the Workspace menu at the top-right "
                "of the Workspace Tree header to add or "
                "select a workspace."
            ),
        )
    
        return False
    
    
    def show_no_workspace_loaded(self):
    
        self.root.title(
            "MCodePatcher - No Workspace Loaded"
        )
    
        if hasattr(self, "workspace_var"):
            self.workspace_name_label.config(
                text=""
            )
    
        if hasattr(self, "workspace_tree"):
    
            self.tree_manager.populate_empty(
                self.workspace_tree,
                self.state.workspace_node_map,
                "No Workspace Loaded",
                "Use the Workspace menu above to add or select a workspace.",
            )
    
        if hasattr(self, "patch_tree"):
    
            self.tree_manager.populate_empty(
                self.patch_tree,
                self.state.patch_node_map,
                "No Patch Tree Loaded",
                "Patch folders generate+link after a workspace is loaded.",
            )
    
        if hasattr(self, "status_text"):
        
            self.status_text.configure(
                state="normal"
            )
        
            self.status_text.delete(
                "1.0",
                tk.END,
            )
        
            self.status_text.insert(
                "1.0",
                (
                    "No workspace loaded.\n\n"
                    "Open the Workspace menu in the Workspace Tree header "
                    "to add or select a workspace."
                )
            )
        
            self.status_text.configure(
                state="disabled"
            )
        if hasattr(self, "activity_list"):
    
            self.activity_list.delete(0, tk.END)

        if hasattr(self, "backup_panel"):

            self.hide_backup_panel()
    
        if hasattr(self, "snippet_tree"):
    
            for tree in (
                self.files_tree,
                self.advanced_tree,
            ):
    
                tree.delete(*tree.get_children())
    
                tree.insert(
                    "",
                    "end",
                    text="No workspace loaded",
                    values=("",),
                    tags=("placeholder",),
                )
    def clamp_main_pane(self, _event=None):
    
        try:
    
            minimum_left = 20
            minimum_right = 20
    
            total_width = self.main_pane.winfo_width()
    
            current = self.main_pane.sashpos(0)
    
            maximum = total_width - minimum_right
    
            if current < minimum_left:
    
                current = minimum_left
    
            if current > maximum:
    
                current = maximum
    
            self.main_pane.sashpos(
                0,
                current,
            )
    
        except Exception:
            pass
        
    def clamp_left_vertical_pane(self, _event=None):
    
        try:
    
            minimum_top = 20
            minimum_bottom = 20
    
            total_height = self.left_vertical_pane.winfo_height()
    
            current = self.left_vertical_pane.sashpos(0)
    
            maximum = total_height - minimum_bottom
    
            if current < minimum_top:
    
                current = minimum_top
    
            if current > maximum:
    
                current = maximum
    
            self.left_vertical_pane.sashpos(
                0,
                current,
            )
    
        except Exception:
            pass
    
    
    def clamp_right_vertical_pane(self, _event=None):
    
        try:
    
            minimum_top = 20
            minimum_bottom = 20
    
            total_height = self.right_vertical_pane.winfo_height()
    
            current = self.right_vertical_pane.sashpos(0)
    
            maximum = total_height - minimum_bottom
    
            if current < minimum_top:
    
                current = minimum_top
    
            if current > maximum:
    
                current = maximum
    
            self.right_vertical_pane.sashpos(
                0,
                current,
            )
    
        except Exception:
            pass
    
    
    def clamp_reference_pane(self, _event=None):
    
        try:
    
            minimum_top = 20
            minimum_bottom = 20
    
            total_height = (
                self.reference_vertical_pane.winfo_height()
            )
    
            if total_height <= 0:
                return
    
            current = (
                self.reference_vertical_pane.sashpos(0)
            )
    
            maximum = (
                total_height - minimum_bottom
            )
    
            clamped = max(
                minimum_top,
                min(current, maximum),
            )
    
            # =====================================
            # ONLY REPOSITION IF NEEDED
            # Prevents geometry thrashing lag
            # =====================================
    
            if clamped != current:
    
                self.reference_vertical_pane.sashpos(
                    0,
                    clamped,
                )
    
        except Exception:
            pass
        
    def clear_focus_on_background_click(self, event):
    
        widget = event.widget
    
        if isinstance(widget, ttk.Entry):
            return
    
        if isinstance(widget, tk.Entry):
            return
    
        self.root.focus_set()


    def suppress_selection_side_effects_temporarily(
        self,
        delay_ms=120,
    ):

        self.selection_side_effect_suppression_active = True

        token = (
            self.selection_side_effect_suppression_token
            + 1
        )

        self.selection_side_effect_suppression_token = token

        try:

            self.root.after(
                delay_ms,
                lambda t=token:
                    self.clear_selection_side_effect_suppression(
                        t
                    ),
            )

        except Exception:

            self.selection_side_effect_suppression_active = False


    def clear_selection_side_effect_suppression(
        self,
        token=None,
    ):

        if (
            token is not None
            and token
            != self.selection_side_effect_suppression_token
        ):
            return

        self.selection_side_effect_suppression_active = False


    def selection_side_effects_suppressed(self):

        return bool(
            getattr(
                self,
                "selection_side_effect_suppression_active",
                False,
            )
        )


    def get_active_version_label(
        self,
        name=None,
        config=None,
    ):

        config = config or CONFIG

        name = name or ACTIVE_MOD

        if not name:
            return ""

        if (
            self.engine
            and name == ACTIVE_MOD
        ):

            return self.engine.generator_folder

        version_key = config.get(
            "active_workspace_versions",
            {},
        ).get(name)

        if version_key:
            return version_key

        return ""


    def format_workspace_display_name(
        self,
        name,
        version=None,
    ):
    
        if not name:
            return ""
    
        display_name = (
            name[:24] + "..."
            if len(name) > 24
            else name
        )
    
        if version:
    
            version = (
                version[:18] + "..."
                if len(version) > 18
                else version
            )
    
            return f"     {display_name} \n   {version}"
    
        return f"[{display_name}]"

    def refresh_workspace_menu(self):
    
        if not hasattr(self, "workspace_menu"):
            return
    
        self.workspace_menu.delete(0, tk.END)

        active_version = self.get_active_version_label()
    
        self.workspace_menu.add_command(
            label=(
                f"Current: {ACTIVE_MOD or 'No Workspace Loaded'}"
                + (
                    f" [{active_version}]"
                    if active_version
                    else ""
                )
            ),
            state="disabled",
        )
    
        self.workspace_menu.add_separator()
    
        for name in sorted(MODS.keys()):
        
            self.workspace_menu.add_command(
                label=name,
                command=lambda n=name: (
                    self.workspace_name_label.config(
                        text=(
                            f"   [{n[:32]}{'...' if len(n) > 32 else ''}]"
                        )
                    ),
                    self.switch_workspace(n),
                ),
            )
    
        self.workspace_menu.add_separator()
    
        self.workspace_menu.add_command(
            label="Add MCreator Workspace",
            command=self.add_workspace,
        )

        self.workspace_menu.add_command(
            label="Manage Workspace Versions",
            command=self.open_workspace_version_manager,
        )
    
        self.workspace_menu.add_command(
            label="Change Patch Root",
            command=self.change_patch_root,
        )
    def open_path(self, path: Path):
    
        if not path:
            return
    
        if not path.exists():
            return
    
        try:
    
            if os.name == "nt":
    
                os.startfile(path)
    
            elif sys.platform == "darwin":
    
                subprocess.Popen(["open", str(path)])
    
            else:
    
                subprocess.Popen(["xdg-open", str(path)])
    
        except Exception as e:
    
            messagebox.showerror(
                "Open Failed",
                str(e),
            )
    def open_selected_advanced_rule(self, _event=None):

        item = None

        if _event is not None:

            try:

                item = self.advanced_tree.identify_row(
                    _event.y
                )

            except Exception:
                item = None

        if not item:

            selection = self.advanced_tree.selection()

            if not selection:
                return "break"

            item = selection[0]

        path = self.get_advanced_rule_path_from_item(
            item
        )

        if not path:
            return "break"

        self.open_advanced_rule_editor(path)

        return "break"


    def get_advanced_rule_path_from_item(
        self,
        item,
    ):

        if not item:
            return None

        current = item

        while current:

            values = self.advanced_tree.item(
                current,
                "values",
            )

            if len(values) >= 4:

                candidate = str(
                    values[3] or ""
                )

                normalized = (
                    str(
                        self.engine
                        .normalized_disabled_path(
                            Path(candidate)
                        )
                    )
                    if self.engine and candidate
                    else candidate
                )

                if normalized.endswith(
                    ".inject.json"
                ):

                    return Path(candidate)

            current = self.advanced_tree.parent(
                current
            )

        return None


    def advanced_rule_basename(
        self,
        path: Path,
    ):

        name = self.engine.normalized_name(path)

        if name.endswith(".inject.json"):
            return name[: -len(".inject.json")]

        return Path(name).stem


    def make_default_advanced_rule(
        self,
        name: str,
    ):

        return {
            "name": name,
            "enabled": True,
            "scope": {
                "mode": "file",
                "target": "",
                "targets": [{
                    "mode": "file",
                    "target": "",
                }],
            },
            "match": {
                "regex": False,
                "flags": [],
                "snippet": [
                    "old code here"
                ],
            },
            "action": {
                "type": "replace_first",
                "replacement": [
                    "new code here"
                ],
            },
        }


    def advanced_rule_value_to_text(
        self,
        value,
    ):

        if isinstance(value, list):
            return "\n".join(
                str(line) for line in value
            )

        if value is None:
            return ""

        return str(value)


    def advanced_rule_text_to_value(
        self,
        value: str,
    ):

        return value.splitlines()


    def load_advanced_rule_for_editor(
        self,
        path: Path,
    ):

        try:

            raw = json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )

            if isinstance(raw, dict):
                return raw

        except Exception as e:

            messagebox.showwarning(
                "Rule Load Warning",
                (
                    "This advanced rule could not be parsed as JSON.\n\n"
                    "The editor opened a safe default instead. Saving "
                    f"will replace the current file.\n\n{e}"
                ),
            )

        return self.make_default_advanced_rule(
            self.advanced_rule_basename(path)
        )


    def get_advanced_rule_editor_targets(
        self,
        rule,
        force_file_default=False,
    ):

        if force_file_default:

            return [{
                "mode": "file",
                "target": "",
            }]

        if self.engine:

            targets = self.engine.get_advanced_scope_targets(
                rule.get(
                    "scope",
                    {},
                )
            )

            if targets:
                return targets

            return [{
                "mode": "file",
                "target": "",
            }]

        return [{
            "mode": "file",
            "target": "",
        }]


    def build_advanced_scope_from_targets(
        self,
        targets,
    ):

        cleaned = []

        for target in targets:

            mode = str(
                target.get(
                    "mode",
                    "file",
                )
                or "file"
            ).lower()

            if mode not in {
                "directory",
                "file",
            }:
                mode = "file"

            target_value = (
                self.engine.normalize_advanced_target(
                    target.get(
                        "target",
                        "",
                    )
                )
                if self.engine
                else str(
                    target.get(
                        "target",
                        "",
                    )
                    or ""
                ).strip()
            )

            if not target_value:
                continue

            cleaned.append({
                "mode": mode,
                "target": target_value,
            })

        if not cleaned:

            cleaned.append({
                "mode": "file",
                "target": "",
            })

        first = cleaned[0]

        return {
            "mode": first["mode"],
            "target": first["target"],
            "targets": cleaned,
        }


    def get_advanced_snippet_file_options(self):

        options = []

        try:

            if not self.engine.injection_root.exists():
                return options

            for path in sorted(
                self.engine.injection_root.glob(
                    "*.txt*"
                ),
                key=lambda p: p.name.lower(),
            ):

                if self.engine.is_disabled(path):
                    continue

                try:

                    relative = path.relative_to(
                        self.engine.injection_root
                    )

                    options.append(
                        str(relative).replace(
                            "\\",
                            "/",
                        )
                    )

                except Exception:

                    options.append(path.name)

        except Exception:
            pass

        return options


    def validate_advanced_target_text(
        self,
        mode: str,
        target: str,
    ):

        mode = str(
            mode or "file"
        ).lower()

        raw_target = str(
            target or ""
        ).strip().replace(
            "\\",
            "/",
        )

        target = (
            self.engine.normalize_advanced_target(
                target
            )
            if self.engine
            else raw_target.strip("/")
        )

        if mode not in {
            "directory",
            "file",
        }:
            raise ValueError(
                "Advanced rule targets must be file or directory."
            )

        if not target:
            raise ValueError(
                "Each file or directory target needs a workspace-relative path."
            )

        if mode == "file" and target == ".":
            raise ValueError(
                "The workspace root can only be used as a directory target."
            )

        if (
            raw_target.startswith("/")
            or re.match(r"^[A-Za-z]:", raw_target)
        ):
            raise ValueError(
                "Advanced rule targets must stay inside the workspace. "
                "Use workspace-relative paths only."
            )

        path = Path(target)

        if ".." in path.parts:
            raise ValueError(
                "Advanced rule targets cannot use '..' to leave the workspace."
            )

        return target


    def workspace_relative_target_from_path(
        self,
        selected,
    ):

        selected_path = Path(selected).resolve()

        workspace_root = (
            self.engine.workspace_root.resolve()
        )

        try:

            relative = selected_path.relative_to(
                workspace_root
            )

        except Exception:

            messagebox.showerror(
                "Target Outside Workspace",
                (
                    "Advanced rule targets must be inside the active "
                    "MCreator workspace."
                ),
            )

            return None

        relative_text = str(relative).replace(
            "\\",
            "/",
        )

        if relative_text in {
            "",
            ".",
        }:
            return "."

        return relative_text


    def open_advanced_rule_editor(
        self,
        rule_path: Path,
        is_new=False,
    ):

        if not self.require_workspace():
            return

        rule_path = Path(rule_path)

        rule = self.load_advanced_rule_for_editor(
            rule_path
        )

        match_cfg = rule.get(
            "match",
            {},
        )

        if not isinstance(match_cfg, dict):
            match_cfg = {}

        action_cfg = rule.get(
            "action",
            {},
        )

        if not isinstance(action_cfg, dict):
            action_cfg = {}

        editor = tk.Toplevel(self.root)

        editor.title(
            (
                "Create Advanced Rule"
                if is_new
                else "Edit Advanced Rule"
            )
        )

        editor.geometry("900x760")
        editor.minsize(760, 620)
        editor.transient(self.root)
        editor.grab_set()

        name_var = tk.StringVar(
            value=rule.get(
                "name",
                self.advanced_rule_basename(
                    rule_path
                ),
            )
        )

        enabled_var = tk.BooleanVar(
            value=rule.get(
                "enabled",
                True,
            )
        )

        regex_var = tk.BooleanVar(
            value=bool(
                match_cfg.get(
                    "regex",
                    False,
                )
            )
        )

        flags = match_cfg.get(
            "flags",
            [],
        )

        if not isinstance(flags, list):
            flags = []

        flag_vars = {
            flag: tk.BooleanVar(
                value=flag in flags
            )
            for flag in (
                "IGNORECASE",
                "MULTILINE",
                "DOTALL",
            )
        }

        action_type_var = tk.StringVar(
            value=action_cfg.get(
                "type",
                "replace_all",
            )
        )

        replacement_source_var = tk.StringVar(
            value=(
                "snippet"
                if action_cfg.get(
                    "replacement_snippet_file"
                )
                else "inline"
            )
        )

        snippet_file_var = tk.StringVar(
            value=action_cfg.get(
                "replacement_snippet_file",
                "",
            )
        )

        outer = ttk.Frame(editor)
        outer.pack(
            fill=tk.BOTH,
            expand=True,
        )

        canvas = tk.Canvas(
            outer,
            highlightthickness=0,
        )

        scrollbar = ttk.Scrollbar(
            outer,
            orient="vertical",
            command=canvas.yview,
        )

        canvas.configure(
            yscrollcommand=scrollbar.set
        )

        scrollbar.pack(
            side=tk.RIGHT,
            fill=tk.Y,
        )

        canvas.pack(
            side=tk.LEFT,
            fill=tk.BOTH,
            expand=True,
        )

        body = ttk.Frame(canvas)

        body_window = canvas.create_window(
            (0, 0),
            window=body,
            anchor="nw",
        )

        def resize_body(event):

            canvas.itemconfigure(
                body_window,
                width=event.width,
            )

        def update_scrollregion(_event=None):

            canvas.configure(
                scrollregion=canvas.bbox("all")
            )

        canvas.bind(
            "<Configure>",
            resize_body,
        )

        body.bind(
            "<Configure>",
            update_scrollregion,
        )

        if is_new:

            ttk.Label(
                body,
                text=(
                    "This rule can be edited later by double-clicking it "
                    "in the Advanced Rule Usage History tab."
                ),
                foreground="#7fdcff",
                wraplength=820,
            ).pack(
                fill=tk.X,
                padx=12,
                pady=(12, 4),
            )

        identity = ttk.LabelFrame(
            body,
            text="Rule",
        )

        identity.pack(
            fill=tk.X,
            padx=12,
            pady=(12, 8),
        )

        ttk.Label(
            identity,
            text="Name",
        ).grid(
            row=0,
            column=0,
            sticky="w",
            padx=8,
            pady=6,
        )

        ttk.Entry(
            identity,
            textvariable=name_var,
        ).grid(
            row=0,
            column=1,
            sticky="ew",
            padx=8,
            pady=6,
        )

        ttk.Checkbutton(
            identity,
            text="Enabled",
            variable=enabled_var,
        ).grid(
            row=0,
            column=2,
            sticky="w",
            padx=8,
            pady=6,
        )

        ttk.Label(
            identity,
            text=str(rule_path),
            foreground="#808080",
        ).grid(
            row=1,
            column=0,
            columnspan=3,
            sticky="w",
            padx=8,
            pady=(0, 6),
        )

        identity.columnconfigure(
            1,
            weight=1,
        )

        targets_frame = ttk.LabelFrame(
            body,
            text="Targets",
        )

        targets_frame.pack(
            fill=tk.X,
            padx=12,
            pady=8,
        )

        ttk.Label(
            targets_frame,
            text=(
                "Rules can target every file inside chosen directories if selected. "
                "Be careful with these, as incorrect mass-replacements could corrupt workspaces. "
                "It is usually recommended to limit targeting to files inside the src/main/java directory as MCreator can regenerate these. "
            ),
            foreground="#b08050",
            wraplength=820,
        ).pack(
            fill=tk.X,
            padx=8,
            pady=(8, 2),
        )

        target_rows_frame = ttk.Frame(
            targets_frame
        )

        target_rows_frame.pack(
            fill=tk.X,
            padx=8,
            pady=(4, 2),
        )

        target_rows = []

        def browse_target(row_data):

            mode = row_data["mode_var"].get()

            initial = str(
                self.engine.workspace_root
            )

            if mode == "file":

                selected = filedialog.askopenfilename(
                    title="Select target file",
                    initialdir=initial,
                )

            else:

                selected = filedialog.askdirectory(
                    title="Select target directory",
                    initialdir=initial,
                )

            if not selected:
                return

            relative = (
                self.workspace_relative_target_from_path(
                    selected
                )
            )

            if relative is None:
                return

            row_data["target_var"].set(relative)

        def refresh_target_row_states(*_args):

            for row_data in target_rows:

                row_data["entry"].configure(
                    state="normal"
                )

                row_data["browse"].configure(
                    state="normal"
                )

        def remove_target_row(row_data):

            if len(target_rows) <= 1:
                return

            target_rows.remove(row_data)
            row_data["frame"].destroy()

        def add_target_row(
            mode="file",
            target="",
        ):

            frame = ttk.Frame(
                target_rows_frame
            )

            frame.pack(
                fill=tk.X,
                pady=2,
            )

            mode_var = tk.StringVar(
                value=mode
            )

            target_var = tk.StringVar(
                value=target
            )

            mode_box = ttk.Combobox(
                frame,
                textvariable=mode_var,
                values=(
                    "file",
                    "directory",
                ),
                state="readonly",
                width=12,
            )

            mode_box.pack(
                side=tk.LEFT,
                padx=(0, 6),
            )

            entry = ttk.Entry(
                frame,
                textvariable=target_var,
            )

            entry.pack(
                side=tk.LEFT,
                fill=tk.X,
                expand=True,
                padx=(0, 6),
            )

            browse = ttk.Button(
                frame,
                text="...",
                width=4,
            )

            browse.pack(
                side=tk.LEFT,
                padx=(0, 6),
            )

            remove = ttk.Button(
                frame,
                text="-",
                width=4,
            )

            remove.pack(
                side=tk.LEFT,
            )

            row_data = {
                "frame": frame,
                "mode_var": mode_var,
                "target_var": target_var,
                "entry": entry,
                "browse": browse,
            }

            browse.configure(
                command=lambda: browse_target(
                    row_data
                )
            )

            remove.configure(
                command=lambda: remove_target_row(
                    row_data
                )
            )

            mode_box.bind(
                "<<ComboboxSelected>>",
                refresh_target_row_states,
            )

            target_rows.append(row_data)
            refresh_target_row_states()

        for target in self.get_advanced_rule_editor_targets(
            rule,
            force_file_default=is_new,
        ):

            add_target_row(
                target.get(
                    "mode",
                    "file",
                ),
                target.get(
                    "target",
                    "",
                ),
            )

        if not target_rows:

            add_target_row()

        ttk.Button(
            targets_frame,
            text="+ Add Target",
            command=add_target_row,
        ).pack(
            anchor="w",
            padx=8,
            pady=(2, 8),
        )

        match_frame = ttk.LabelFrame(
            body,
            text="Match",
        )

        match_frame.pack(
            fill=tk.BOTH,
            expand=True,
            padx=12,
            pady=8,
        )

        match_options = ttk.Frame(
            match_frame
        )

        match_options.pack(
            fill=tk.X,
            padx=8,
            pady=(8, 2),
        )

        ttk.Checkbutton(
            match_options,
            text="Regex",
            variable=regex_var,
        ).pack(
            side=tk.LEFT,
            padx=(0, 14),
        )

        for flag, var in flag_vars.items():

            ttk.Checkbutton(
                match_options,
                text=flag,
                variable=var,
            ).pack(
                side=tk.LEFT,
                padx=(0, 10),
            )

        match_text = tk.Text(
            match_frame,
            height=9,
            wrap="none",
            undo=True,
        )

        match_text.insert(
            "1.0",
            self.advanced_rule_value_to_text(
                match_cfg.get(
                    "snippet",
                    "",
                )
            ),
        )

        match_text.pack(
            fill=tk.BOTH,
            expand=True,
            padx=8,
            pady=(2, 8),
        )

        action_frame = ttk.LabelFrame(
            body,
            text="Action",
        )

        action_frame.pack(
            fill=tk.BOTH,
            expand=True,
            padx=12,
            pady=8,
        )

        action_row = ttk.Frame(
            action_frame
        )

        action_row.pack(
            fill=tk.X,
            padx=8,
            pady=(8, 4),
        )

        ttk.Label(
            action_row,
            text="Type",
        ).pack(
            side=tk.LEFT,
            padx=(0, 6),
        )

        ttk.Combobox(
            action_row,
            textvariable=action_type_var,
            values=(
                "replace_first",
                "replace_all",
            ),
            state="readonly",
            width=16,
        ).pack(
            side=tk.LEFT,
            padx=(0, 18),
        )

        ttk.Radiobutton(
            action_row,
            text="Inline Replacement",
            variable=replacement_source_var,
            value="inline",
        ).pack(
            side=tk.LEFT,
            padx=(0, 12),
        )

        ttk.Radiobutton(
            action_row,
            text="Snippet File",
            variable=replacement_source_var,
            value="snippet",
        ).pack(
            side=tk.LEFT,
        )

        snippet_row = ttk.Frame(
            action_frame
        )

        snippet_row.pack(
            fill=tk.X,
            padx=8,
            pady=(0, 4),
        )

        ttk.Label(
            snippet_row,
            text="Snippet",
        ).pack(
            side=tk.LEFT,
            padx=(0, 6),
        )

        snippet_box = ttk.Combobox(
            snippet_row,
            textvariable=snippet_file_var,
            values=tuple(
                self.get_advanced_snippet_file_options()
            ),
        )

        snippet_box.pack(
            side=tk.LEFT,
            fill=tk.X,
            expand=True,
            padx=(0, 6),
        )

        def browse_snippet_file():

            selected = filedialog.askopenfilename(
                title="Select snippet file",
                initialdir=str(
                    self.engine.injection_root
                ),
                filetypes=(
                    ("Text files", "*.txt"),
                    ("All files", "*"),
                ),
            )

            if not selected:
                return

            try:

                relative = Path(selected).relative_to(
                    self.engine.injection_root
                )

                snippet_file_var.set(
                    str(relative).replace(
                        "\\",
                        "/",
                    )
                )

            except Exception:

                snippet_file_var.set(
                    Path(selected).name
                )

        snippet_browse_button = ttk.Button(
            snippet_row,
            text="...",
            width=4,
            command=browse_snippet_file,
        )

        snippet_browse_button.pack(
            side=tk.LEFT,
        )

        replacement_text = tk.Text(
            action_frame,
            height=9,
            wrap="none",
            undo=True,
        )

        replacement_text.insert(
            "1.0",
            self.advanced_rule_value_to_text(
                action_cfg.get(
                    "replacement",
                    "",
                )
            ),
        )

        replacement_text.pack(
            fill=tk.BOTH,
            expand=True,
            padx=8,
            pady=(2, 8),
        )

        def update_replacement_source_state(*_args):

            def resize_editor_height(height):

                try:

                    editor.update_idletasks()

                    width = max(
                        editor.winfo_width(),
                        900,
                    )

                    editor.geometry(
                        f"{width}x{height}"
                    )

                    update_scrollregion()

                except Exception:
                    pass

            if replacement_source_var.get() == "snippet":

                if replacement_text.winfo_ismapped():

                    replacement_text.pack_forget()

                snippet_box.configure(
                    state="normal"
                )

                snippet_browse_button.configure(
                    state="normal"
                )

                editor.minsize(
                    760,
                    520,
                )

                editor.after_idle(
                    lambda: resize_editor_height(620)
                )

            else:

                if not replacement_text.winfo_ismapped():

                    replacement_text.pack(
                        fill=tk.BOTH,
                        expand=True,
                        padx=8,
                        pady=(2, 8),
                    )

                snippet_box.configure(
                    state="disabled"
                )

                snippet_browse_button.configure(
                    state="disabled"
                )

                editor.minsize(
                    760,
                    620,
                )

                editor.after_idle(
                    lambda: resize_editor_height(760)
                )

        replacement_source_var.trace_add(
            "write",
            update_replacement_source_state,
        )

        update_replacement_source_state()

        button_row = ttk.Frame(editor)

        button_row.pack(
            fill=tk.X,
            padx=12,
            pady=10,
        )

        def collect_rule():

            name = name_var.get().strip()

            if not name:
                raise ValueError(
                    "Rule name is required."
                )

            targets = []

            for row_data in target_rows:

                mode = row_data[
                    "mode_var"
                ].get()

                target = row_data[
                    "target_var"
                ].get().strip()

                target = self.validate_advanced_target_text(
                    mode,
                    target,
                )

                if not target:

                    raise ValueError(
                        "Each file or directory target needs a path."
                    )

                targets.append({
                    "mode": mode,
                    "target": target,
                })

            match_value = match_text.get(
                "1.0",
                "end-1c",
            ).rstrip("\n")

            if not match_value.strip():
                raise ValueError(
                    "Match snippet is required."
                )

            selected_flags = [
                flag for flag, var
                in flag_vars.items()
                if var.get()
            ]

            if regex_var.get():

                regex_flags = 0

                if "IGNORECASE" in selected_flags:
                    regex_flags |= re.IGNORECASE

                if "MULTILINE" in selected_flags:
                    regex_flags |= re.MULTILINE

                if "DOTALL" in selected_flags:
                    regex_flags |= re.DOTALL

                re.compile(
                    match_value,
                    regex_flags,
                )

            updated = dict(rule)

            updated["name"] = name
            updated["enabled"] = bool(
                enabled_var.get()
            )

            existing_match = updated.get(
                "match",
                {},
            )

            match_updated = (
                dict(existing_match)
                if isinstance(
                    existing_match,
                    dict,
                )
                else {}
            )

            match_updated["regex"] = bool(
                regex_var.get()
            )

            match_updated["flags"] = (
                selected_flags
            )

            match_updated["snippet"] = (
                self.advanced_rule_text_to_value(
                    match_value
                )
            )

            updated["match"] = match_updated

            updated["scope"] = (
                self.build_advanced_scope_from_targets(
                    targets
                )
            )

            existing_action = updated.get(
                "action",
                {},
            )

            action_updated = (
                dict(existing_action)
                if isinstance(
                    existing_action,
                    dict,
                )
                else {}
            )

            action_updated["type"] = (
                action_type_var.get()
                if action_type_var.get()
                in {
                    "replace_first",
                    "replace_all",
                }
                else "replace_all"
            )

            if replacement_source_var.get() == "snippet":

                snippet_file = (
                    snippet_file_var.get().strip()
                )

                if not snippet_file:
                    raise ValueError(
                        "Snippet replacement file is required."
                    )

                action_updated[
                    "replacement_snippet_file"
                ] = snippet_file

                action_updated.pop(
                    "replacement",
                    None,
                )

            else:

                replacement_value = (
                    replacement_text.get(
                        "1.0",
                        "end-1c",
                    ).rstrip("\n")
                )

                action_updated["replacement"] = (
                    self.advanced_rule_text_to_value(
                        replacement_value
                    )
                )

                action_updated.pop(
                    "replacement_snippet_file",
                    None,
                )

            updated["action"] = action_updated

            return updated

        def save_rule(close_after=False):

            try:

                updated = collect_rule()

                rule_path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                self.engine.atomic_write(
                    rule_path,
                    json.dumps(
                        updated,
                        indent=4,
                    ),
                )

                rule.clear()
                rule.update(updated)

                self.engine.rebuild_advanced_rule_cache()

                self.queue_advanced_rule_candidates(
                    rule_path=rule_path,
                    reason="ADVANCED_RULE_SAVE",
                )

                self.schedule_refresh(
                    scope=REFRESH_FULL
                )

                self.select_path_in_tree(
                    self.patch_tree,
                    rule_path,
                    additive=False,
                )

                self.log.log(
                    f"Advanced rule saved: {rule_path.name}"
                )

                if close_after:
                    editor.destroy()

            except Exception as e:

                messagebox.showerror(
                    "Save Advanced Rule",
                    str(e),
                    parent=editor,
                )

        ttk.Button(
            button_row,
            text="Open JSON",
            command=lambda: self.open_path(rule_path),
        ).pack(
            side=tk.LEFT,
        )

        ttk.Button(
            button_row,
            text="Cancel",
            command=editor.destroy,
        ).pack(
            side=tk.RIGHT,
            padx=(8, 0),
        )

        ttk.Button(
            button_row,
            text="Save & Close",
            command=lambda: save_rule(True),
        ).pack(
            side=tk.RIGHT,
            padx=(8, 0),
        )

        ttk.Button(
            button_row,
            text="Save",
            command=lambda: save_rule(False),
        ).pack(
            side=tk.RIGHT,
        )

        editor.bind(
            "<Escape>",
            lambda _event: editor.destroy(),
        )

        editor.focus_set()
            
    def switch_workspace(self, selected):
    
        if selected == ACTIVE_MOD:
            return
    
        self.reload_workspace(selected)

    def reload_workspace(
        self,
        selected,
        queue_reconcile=False,
        reason="LOAD",
    ):
    
        global ACTIVE_MOD
        global CONFIG
        global MODS
        global CUSTOM_PATCH_ROOT
    
        CONFIG = load_config()
    
        ACTIVE_MOD = selected
    
        CONFIG["active_mod"] = selected
    
        save_config(CONFIG)
    
        MODS = build_mod_map(CONFIG)
    
        CUSTOM_PATCH_ROOT = Path(
            CONFIG.get(
                "patch_root",
                str(Path.home() / "MCodePatcher"),
            )
        )
    
        if selected not in MODS:
    
            messagebox.showerror(
                "Workspace Missing",
                f"Workspace '{selected}' is not configured.",
            )
    
            return

        try:

            startup_identity = detect_workspace_identity(
                MODS[selected],
                log_callback=self.log.log,
            )

            self.sync_active_workspace_version_to_detected(
                selected,
                MODS[selected],
                config=CONFIG,
                identity=startup_identity,
            )

        except Exception as e:

            self.log.log(
                f"Workspace version sync failed: {e}"
            )
    
        self.log.log(
            f"Loading workspace: {selected}"
        )

        if hasattr(self, "loading_var"):

            self.loading_var.set(
                "Loading workspace..."
            )
    
        try:

            # Invalidate any background refresh from the
            # previous workspace before swapping engines.
            self.refresh_generation += 1
    
            if (
                self.observer
                and self.observer.is_alive()
            ):
    
                self.observer.stop()
                self.observer.join(timeout=2)
    
            self.state.pending_files.clear()
            self.state.recently_patched.clear()
            self.state.out_of_sync_attempts.clear()
            self.state.last_seen_hashes.clear()

            if hasattr(self, "backup_panel"):

                self.hide_backup_panel()
    
            self.engine = PatchEngine(self)
    
            self.engine.ensure_patch_roots()
    
            self.setup_watcher()
    
            self.root.title(
                (
                    f"MCodePatcher - {ACTIVE_MOD} "
                    f"[{self.engine.generator_folder}]"
                )
            )
    
            if hasattr(self, "workspace_var"):

                self.workspace_name_label.config(
                    text=self.format_workspace_display_name(
                        ACTIVE_MOD,
                        self.engine.generator_folder,
                    )
                )
    
            self.refresh_workspace_menu()

            if queue_reconcile:

                self.queue_workspace_patch_candidates(
                    reason=reason
                )
    
            self.schedule_refresh(
                scope=REFRESH_FULL
            )
    
            self.log.log(
                f"Workspace loaded: {selected} "
                f"[{self.engine.generator_folder}]"
            )
    
        except Exception as e:
    
            self.log.log(
                f"Workspace load failed: {e}"
            )


    def schedule_workspace_identity_check(
        self,
        changed_path=None,
        event_type="MODIFIED",
    ):

        if not self.engine:
            return

        if changed_path:

            self.log.log(
                "Workspace Gradle identity changed: "
                f"{Path(changed_path).name} ({event_type})"
            )

        if self.workspace_identity_after:

            try:

                self.root.after_cancel(
                    self.workspace_identity_after
                )

            except Exception:
                pass

        self.workspace_identity_after = self.root.after(
            750,
            self.check_workspace_identity,
        )


    def check_workspace_identity(self):

        self.workspace_identity_after = None

        if not self.engine or not ACTIVE_MOD:
            return

        old_folder = self.engine.generator_folder

        identity = detect_workspace_identity(
            self.engine.workspace_root,
            log_callback=self.log.log,
        )

        new_generator = identity["generator"]
        new_version = identity["mc_version"]
        new_folder = identity["generator_folder"]

        if (
            new_generator == "unknown"
            or new_version == "UNKNOWN"
        ):

            self.log.log(
                "Generator/version check incomplete; "
                f"keeping active patch tree: {old_folder}"
            )

            return

        if new_folder == old_folder:
            return
    
        self.log.log(
            "Generator/version changed: "
            f"{old_folder} -> {new_folder}"
        )

        config = load_config()

        self.sync_active_workspace_version_to_detected(
            ACTIVE_MOD,
            self.engine.workspace_root,
            config=config,
            identity=identity,
        )

        self.reload_workspace(
            ACTIVE_MOD,
            queue_reconcile=True,
            reason="GENERATOR_SWITCH",
        )


    def queue_workspace_patch_candidates(
        self,
        reason="RECONCILE",
    ):

        if not self.engine:
            return 0

        queued = 0
        changed_paths = []

        for workspace_path in self.engine.iter_workspace_files():

            if not workspace_path.is_file():
                continue

            if workspace_path.name in IGNORED_FILENAMES:
                continue

            if (
                self.engine.get_real_suffix(workspace_path)
                not in ALLOWED_EXTENSIONS
            ):
                continue

            try:

                relative = str(
                    workspace_path.relative_to(
                        self.engine.workspace_root
                    )
                ).replace("\\", "/")

            except Exception:
                continue

            override_path = self.engine.to_patch_path(
                workspace_path
            )

            has_override = override_path.exists()

            has_markers = False

            try:

                content = workspace_path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                )

                has_markers = (
                    self.engine.content_has_any_patch_marker(
                        content,
                        include_applied=True,
                    )
                )

            except Exception:
                pass

            has_advanced_rules = bool(
                self.engine.get_advanced_rules_for_file(
                    relative
                )
            )

            if not (
                has_override
                or has_markers
                or has_advanced_rules
            ):
                continue

            with self.lock:

                self.state.pending_files[
                    str(workspace_path)
                ] = PendingPatch(
                    changed_file=workspace_path,
                    override_path=override_path,
                    event_type=reason,
                )

            changed_paths.append(workspace_path)
            queued += 1

        if not queued:
            return 0

        self.last_event_time = time.time()

        self.activity_queue.put(
            f"[{reason}] queued {queued} files"
        )

        self.schedule_refresh(
            scope=REFRESH_FULL,
            changed_paths=changed_paths,
        )

        self.log.log(
            f"{reason}: queued {queued} patch candidates"
        )

        return queued


    def queue_advanced_rule_candidates(
        self,
        rule_path=None,
        reason="ADVANCED_RULE_CHANGE",
    ):

        if not self.engine:
            return 0

        normalized_rule_path = None

        if rule_path:

            try:

                normalized_rule_path = str(
                    self.engine.normalized_disabled_path(
                        Path(rule_path)
                    )
                )

            except Exception:

                normalized_rule_path = str(rule_path)

        queued = 0
        changed_paths = []

        for workspace_path in self.engine.iter_workspace_files():

            if not workspace_path.is_file():
                continue

            if workspace_path.name in IGNORED_FILENAMES:
                continue

            if (
                self.engine.get_real_suffix(workspace_path)
                not in ALLOWED_EXTENSIONS
            ):
                continue

            try:

                relative = str(
                    workspace_path.relative_to(
                        self.engine.workspace_root
                    )
                ).replace("\\", "/")

            except Exception:
                continue

            rules = self.engine.get_advanced_rules_for_file(
                relative
            )

            if normalized_rule_path:

                filtered_rules = []

                for rule in rules:

                    try:

                        source = str(
                            self.engine.normalized_disabled_path(
                                Path(
                                    rule.get(
                                        "_source_file",
                                        "",
                                    )
                                )
                            )
                        )

                    except Exception:

                        source = str(
                            rule.get(
                                "_source_file",
                                "",
                            )
                        )

                    if source == normalized_rule_path:
                        filtered_rules.append(rule)

                rules = filtered_rules

            if not rules:
                continue

            override_path = self.engine.to_patch_path(
                workspace_path
            )

            with self.lock:

                self.state.pending_files[
                    str(workspace_path)
                ] = PendingPatch(
                    changed_file=workspace_path,
                    override_path=override_path,
                    event_type=reason,
                )

            changed_paths.append(workspace_path)
            queued += 1

        self.last_event_time = time.time()

        if queued:

            self.activity_queue.put(
                f"[{reason}] queued {queued} advanced files"
            )

            self.schedule_refresh(
                scope=REFRESH_PARTIAL,
                changed_paths=changed_paths,
            )

        self.log.log(
            f"{reason}: queued {queued} advanced candidates"
        )

        return queued


    def refresh_runtime_workspace_config(
        self,
        config=None,
    ):

        global CONFIG
        global MODS
        global CUSTOM_PATCH_ROOT

        CONFIG = config or load_config()

        MODS = build_mod_map(CONFIG)

        CUSTOM_PATCH_ROOT = Path(
            CONFIG.get(
                "patch_root",
                str(Path.home() / "MCodePatcher"),
            )
        )


    def make_workspace_version_record(
        self,
        folder,
        version_key=None,
    ):

        path = Path(folder)

        identity = detect_workspace_identity(
            path,
            log_callback=self.log.log,
        )

        key = version_key or identity[
            "generator_folder"
        ]

        return key, {
            "path": str(path),
            "generator": identity["generator"],
            "mc_version": identity["mc_version"],
            "detected_key": identity["generator_folder"],
            "label": key,
            "updated": time.time(),
        }


    def sync_active_workspace_version_to_detected(
        self,
        mod_name,
        workspace_root,
        config=None,
        identity=None,
    ):

        config = config or load_config()

        workspace_root = Path(workspace_root)

        identity = identity or detect_workspace_identity(
            workspace_root,
            log_callback=self.log.log,
        )

        detected_key = identity["generator_folder"]

        if (
            identity["generator"] == "unknown"
            or identity["mc_version"] == "UNKNOWN"
        ):
            return False

        versions = config.setdefault(
            "workspace_versions",
            {},
        ).setdefault(
            mod_name,
            {},
        )

        active_versions = config.setdefault(
            "active_workspace_versions",
            {},
        )

        previous_active_key = active_versions.get(
            mod_name
        )

        existing = versions.get(detected_key)

        previous_record = (
            dict(existing)
            if isinstance(existing, dict)
            else existing
        )

        existing_path = get_workspace_version_entry_path(
            existing
        )

        if (
            existing_path
            and not paths_match(
                existing_path,
                workspace_root,
            )
        ):

            self.log.log(
                "Detected active workspace version "
                f"{detected_key}, but that version is already "
                "linked to a different workspace root. Using "
                "the detected patch tree for this session; "
                "repoint the version link in the manager to "
                "save it."
            )

            return False

        changed = (
            previous_active_key != detected_key
        )

        detected_key, record = (
            update_workspace_version_record_for_identity(
                config,
                mod_name,
                workspace_root,
                identity,
            )
        )

        if previous_record != record:
            changed = True

        if changed:

            save_config(config)

            self.refresh_runtime_workspace_config(
                config
            )

            self.log.log(
                "Active workspace version "
                + (
                    f"auto-switched: {previous_active_key} -> "
                    if previous_active_key
                    and previous_active_key != detected_key
                    else "synced to "
                )
                + f"{detected_key}"
            )

        return changed


    def ensure_workspace_version_entries(
        self,
        mod_name,
        config=None,
    ):

        config = config or CONFIG

        versions = config.setdefault(
            "workspace_versions",
            {},
        ).setdefault(
            mod_name,
            {},
        )

        primary_path = get_mod_root_path(
            config,
            mod_name,
        )

        if primary_path:

            key, record = (
                self.make_workspace_version_record(
                    primary_path
                )
            )

            if key not in versions:

                record["base_workspace"] = True

                versions[key] = record

            else:

                entry = versions[key]

                if isinstance(entry, dict):

                    entry.setdefault(
                        "generator",
                        record["generator"],
                    )

                    entry.setdefault(
                        "mc_version",
                        record["mc_version"],
                    )

                    entry.setdefault(
                        "detected_key",
                        record["detected_key"],
                    )

                    entry.setdefault(
                        "label",
                        key,
                    )

                    entry.setdefault(
                        "base_workspace",
                        True,
                    )

        if (
            mod_name not in config.setdefault(
                "active_workspace_versions",
                {},
            )
            and primary_path
        ):

            identity = detect_workspace_identity(
                primary_path,
                log_callback=self.log.log,
            )

            config[
                "active_workspace_versions"
            ][mod_name] = identity[
                "generator_folder"
            ]

        return versions


    def get_patch_folder_keys_for_mod(
        self,
        mod_name,
    ):

        root = CUSTOM_PATCH_ROOT / mod_name

        if not root.exists():
            return []

        keys = []

        try:

            for child in sorted(
                root.iterdir(),
                key=lambda p: p.name.lower(),
            ):

                if not child.is_dir():
                    continue

                if child.name == BACKUP_FOLDER_NAME:
                    continue

                if child.name.startswith("."):
                    continue

                keys.append(child.name)

        except Exception:
            pass

        return keys


    def merge_patch_folder(
        self,
        source_key,
        target_key,
        overwrite=False,
    ):

        if not ACTIVE_MOD:
            raise RuntimeError(
                "No workspace is active."
            )

        if source_key == target_key:
            raise RuntimeError(
                "Source and target patch folders are the same."
            )

        source_root = (
            CUSTOM_PATCH_ROOT /
            ACTIVE_MOD /
            source_key
        )

        target_root = (
            CUSTOM_PATCH_ROOT /
            ACTIVE_MOD /
            target_key
        )

        if not source_root.exists():
            raise RuntimeError(
                f"Source patch folder does not exist: {source_key}"
            )

        copied = 0
        skipped = 0

        for root, dirs, files in os.walk(source_root):

            root_path = Path(root)

            dirs[:] = [
                d for d in dirs
                if d != "__pycache__"
            ]

            relative = root_path.relative_to(
                source_root
            )

            target_dir = target_root / relative

            target_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            for file in files:

                source_file = root_path / file

                target_file = target_dir / file

                if (
                    target_file.exists()
                    and not overwrite
                ):

                    skipped += 1
                    continue

                shutil.copy2(
                    source_file,
                    target_file,
                )

                copied += 1

        return copied, skipped


    def open_workspace_version_manager(self):

        if not self.require_workspace():
            return

        if not ACTIVE_MOD:
            return

        config = load_config()

        self.ensure_workspace_version_entries(
            ACTIVE_MOD,
            config,
        )

        save_config(config)
        self.refresh_runtime_workspace_config(config)

        editor = tk.Toplevel(self.root)

        editor.title(
            "Workspace Versions and Patch Merge"
        )

        editor.geometry("1040x720")
        editor.minsize(880, 600)
        editor.transient(self.root)

        outer = ttk.Frame(editor)

        outer.pack(
            fill=tk.BOTH,
            expand=True,
            padx=12,
            pady=12,
        )

        ttk.Label(
            outer,
            text=(
                "Link generator/version patch folders to different "
                "MCreator workspace directories, then switch between "
                "them inside the same saved MCodePatcher workspace."
            ),
            wraplength=960,
            foreground="#7fdcff",
        ).pack(
            fill=tk.X,
            pady=(0, 8),
        )

        ttk.Label(
            outer,
            text=(
                "This is useful for ports: for example, keep one logical "
                "workspace here while pointing forge_1201, neoforge_1204, "
                "or neoforge_1211 at separate exported/imported MCreator "
                "workspace folders. Note that versions within the same workspace path will switch automatically."
                " The generator/version currently detected from each linked "
                "folder is authoritative; stale links are shown but cannot "
                "be activated until they are repointed."
            ),
            wraplength=960,
            foreground="#808080",
        ).pack(
            fill=tk.X,
            pady=(0, 12),
        )

        main_pane = ttk.PanedWindow(
            outer,
            orient=tk.VERTICAL,
        )

        main_pane.pack(
            fill=tk.BOTH,
            expand=True,
        )

        version_frame = ttk.LabelFrame(
            main_pane,
            text="Linked Workspace Version Roots",
        )

        main_pane.add(
            version_frame,
            weight=3,
        )

        version_tree = ttk.Treeview(
            version_frame,
            columns=(
                "active",
                "detected",
                "path",
                "patch",
            ),
            show="tree headings",
            selectmode="browse",
            height=10,
        )

        version_tree.heading(
            "#0",
            text="Version Folder",
        )

        version_tree.heading(
            "active",
            text="Active",
        )

        version_tree.heading(
            "detected",
            text="Detected",
        )

        version_tree.heading(
            "path",
            text="Workspace Path",
        )

        version_tree.heading(
            "patch",
            text="Patch Folder",
        )

        version_tree.column(
            "#0",
            width=180,
        )

        version_tree.column(
            "active",
            width=70,
            anchor="center",
        )

        version_tree.column(
            "detected",
            width=180,
        )

        version_tree.column(
            "path",
            width=480,
        )

        version_tree.column(
            "patch",
            width=90,
            anchor="center",
        )

        version_scroll = ttk.Scrollbar(
            version_frame,
            orient="vertical",
            command=version_tree.yview,
        )

        version_tree.configure(
            yscrollcommand=version_scroll.set
        )

        version_scroll.pack(
            side=tk.RIGHT,
            fill=tk.Y,
        )

        version_tree.pack(
            side=tk.LEFT,
            fill=tk.BOTH,
            expand=True,
            padx=6,
            pady=6,
        )

        version_tree.tag_configure(
            "active",
            foreground="#45ff83",
        )

        version_tree.tag_configure(
            "missing",
            foreground="#ff5c5c",
        )

        version_tree.tag_configure(
            "unlinked",
            foreground="#808080",
        )

        version_tree.tag_configure(
            "mismatch",
            foreground="#909090",
        )

        controls = ttk.Frame(
            outer
        )

        controls.pack(
            fill=tk.X,
            pady=(8, 8),
        )

        merge_frame = ttk.LabelFrame(
            main_pane,
            text="Copy / Merge Patch Folder",
        )

        main_pane.add(
            merge_frame,
            weight=1,
        )

        merge_body = ttk.Frame(
            merge_frame
        )

        merge_body.pack(
            fill=tk.X,
            padx=8,
            pady=8,
        )

        source_var = tk.StringVar()
        target_var = tk.StringVar()
        overwrite_var = tk.BooleanVar(value=False)

        ttk.Label(
            merge_body,
            text="From",
        ).grid(
            row=0,
            column=0,
            sticky="w",
            padx=4,
            pady=4,
        )

        source_box = ttk.Combobox(
            merge_body,
            textvariable=source_var,
            state="readonly",
        )

        source_box.grid(
            row=0,
            column=1,
            sticky="ew",
            padx=4,
            pady=4,
        )

        ttk.Label(
            merge_body,
            text="To",
        ).grid(
            row=0,
            column=2,
            sticky="w",
            padx=(16, 4),
            pady=4,
        )

        target_box = ttk.Combobox(
            merge_body,
            textvariable=target_var,
            state="readonly",
        )

        target_box.grid(
            row=0,
            column=3,
            sticky="ew",
            padx=4,
            pady=4,
        )

        ttk.Checkbutton(
            merge_body,
            text="Overwrite existing files",
            variable=overwrite_var,
        ).grid(
            row=1,
            column=1,
            columnspan=2,
            sticky="w",
            padx=4,
            pady=4,
        )

        merge_body.columnconfigure(1, weight=1)
        merge_body.columnconfigure(3, weight=1)

        def get_selected_version_key():

            selection = version_tree.selection()

            if not selection:
                return ""

            return version_tree.item(
                selection[0],
                "text",
            )

        def get_live_workspace_identity_for_entry(
            entry,
        ):

            if isinstance(entry, str):
                entry = {"path": entry}

            path = (
                entry.get("path", "")
                if isinstance(entry, dict)
                else ""
            )

            if not path or not Path(path).exists():
                return None

            try:

                return detect_workspace_identity(
                    Path(path),
                    log_callback=self.log.log,
                )

            except Exception:

                return None

        def selected_version_is_usable(
            key=None,
            notify=False,
        ):

            key = key or get_selected_version_key()

            if not key:
                return False

            config_check = load_config()

            versions_check = (
                config_check.get(
                    "workspace_versions",
                    {},
                ).get(
                    ACTIVE_MOD,
                    {},
                )
            )

            entry = versions_check.get(key)

            if isinstance(entry, str):
                entry = {"path": entry}

            if not entry or not entry.get("path"):

                if notify:

                    messagebox.showinfo(
                        "No Workspace Root Linked",
                        (
                            "This patch folder does not have a linked "
                            "workspace root yet. Use Repoint Selected first."
                        ),
                        parent=editor,
                    )

                return False

            path = Path(entry["path"])

            if not path.exists():

                if notify:

                    messagebox.showerror(
                        "Workspace Root Missing",
                        entry["path"],
                        parent=editor,
                    )

                return False

            identity = get_live_workspace_identity_for_entry(
                entry
            )

            if not identity:

                if notify:

                    messagebox.showerror(
                        "Workspace Detection Failed",
                        (
                            "The selected workspace root could not "
                            "be inspected."
                        ),
                        parent=editor,
                    )

                return False

            detected_key = identity["generator_folder"]

            if (
                identity["generator"] == "unknown"
                or identity["mc_version"] == "UNKNOWN"
            ):

                if notify:

                    messagebox.showerror(
                        "Workspace Version Unknown",
                        (
                            "The selected folder does not expose a "
                            "detectable generator and Minecraft version."
                        ),
                        parent=editor,
                    )

                return False

            if detected_key != key:

                if notify:

                    messagebox.showwarning(
                        "Version Link Is Stale",
                        (
                            f"The selected row is {key}, but its linked "
                            f"workspace currently loads as {detected_key}.\n\n"
                            "MCodePatcher will only activate the version "
                            "that is currently detected from the workspace's "
                            "build files. Repoint this row to a matching "
                            "workspace, or use the detected version row."
                        ),
                        parent=editor,
                    )

                return False

            return True

        def update_version_button_state(
            _event=None,
        ):

            try:

                use_button.configure(
                    state=(
                        tk.NORMAL
                        if selected_version_is_usable()
                        else tk.DISABLED
                    )
                )

            except Exception:
                pass

        def refresh_manager():

            nonlocal config

            config = load_config()

            self.ensure_workspace_version_entries(
                ACTIVE_MOD,
                config,
            )

            save_config(config)
            self.refresh_runtime_workspace_config(
                config
            )

            version_tree.delete(
                *version_tree.get_children()
            )

            versions = config.get(
                "workspace_versions",
                {},
            ).get(
                ACTIVE_MOD,
                {},
            )

            patch_keys = set(
                self.get_patch_folder_keys_for_mod(
                    ACTIVE_MOD
                )
            )

            active_key = config.get(
                "active_workspace_versions",
                {},
            ).get(ACTIVE_MOD)

            actual_active_key = (
                self.engine.generator_folder
                if self.engine
                else active_key
            )

            all_keys = sorted(
                set(versions.keys()) | patch_keys,
                key=lambda x: x.lower(),
            )

            for key in all_keys:

                entry = versions.get(key, {})

                if isinstance(entry, str):

                    entry = {
                        "path": entry,
                    }

                path = entry.get(
                    "path",
                    "",
                )

                detected = entry.get(
                    "detected_key",
                    key,
                )

                live_identity = (
                    get_live_workspace_identity_for_entry(
                        entry
                    )
                    if path
                    else None
                )

                usable = False

                if live_identity:

                    detected = live_identity[
                        "generator_folder"
                    ]

                    usable = (
                        live_identity["generator"] != "unknown"
                        and live_identity["mc_version"] != "UNKNOWN"
                        and detected == key
                    )

                same_live_workspace = False

                if self.engine and path:

                    try:

                        same_live_workspace = paths_match(
                            path,
                            self.engine.workspace_root,
                        )

                    except Exception:
                        same_live_workspace = False

                if (
                    same_live_workspace
                    and live_identity
                    and not usable
                ):

                    detected = f"{detected} (current)"

                elif entry.get("generator") or entry.get(
                    "mc_version"
                ):

                    detected = (
                        f"{entry.get('generator', 'unknown')}_"
                        f"{entry.get('mc_version', 'UNKNOWN')}"
                    )

                patch_exists = (
                    "yes"
                    if key in patch_keys
                    else "no"
                )

                active = (
                    "yes"
                    if key == actual_active_key
                    else (
                        "stale"
                        if same_live_workspace
                        and live_identity
                        and not usable
                        else ""
                    )
                )

                tags = []

                if not path:
                    tags.append("unlinked")

                elif not Path(path).exists():
                    tags.append("missing")

                elif not usable:
                    tags.append("mismatch")

                if active and "mismatch" not in tags:
                    tags.append("active")

                version_tree.insert(
                    "",
                    "end",
                    text=key,
                    values=(
                        active,
                        detected,
                        path,
                        patch_exists,
                    ),
                    tags=tuple(tags),
                )

            merge_keys = all_keys

            source_box.configure(
                values=merge_keys
            )

            target_box.configure(
                values=merge_keys
            )

            if (
                source_var.get()
                not in merge_keys
                and merge_keys
            ):

                source_var.set(merge_keys[0])

            if (
                target_var.get()
                not in merge_keys
                and len(merge_keys) > 1
            ):

                target_var.set(merge_keys[1])

            update_version_button_state()

        def add_or_link_workspace_root(
            version_key=None,
        ):

            folder = filedialog.askdirectory(
                title="Select Linked MCreator Workspace Folder",
                parent=editor,
            )

            if not folder:
                return

            if not self.confirm_workspace_folder(
                folder
            ):
                return

            nonlocal config

            config = load_config()

            versions = self.ensure_workspace_version_entries(
                ACTIVE_MOD,
                config,
            )

            key, record = (
                self.make_workspace_version_record(
                    folder,
                    version_key=version_key,
                )
            )

            if (
                version_key
                and record.get("detected_key") != version_key
            ):

                messagebox.showwarning(
                    "Workspace Version Mismatch",
                    (
                        f"You selected {version_key}, but this "
                        f"workspace currently loads as "
                        f"{record.get('detected_key')}.\n\n"
                        "A version row can only point to a workspace "
                        "whose build files currently match that version. "
                        "Use Add / Link Workspace Root to create or "
                        "update the detected version row instead."
                    ),
                    parent=editor,
                )

                return

            if (
                not version_key
                and key in versions
            ):

                replace = messagebox.askyesno(
                    "Replace Version Link",
                    (
                        f"{key} already has a linked workspace root.\n\n"
                        "Replace it with the selected folder?"
                    ),
                    parent=editor,
                )

                if not replace:
                    return

            versions[key] = record

            config.setdefault(
                "active_workspace_versions",
                {},
            )[ACTIVE_MOD] = key

            save_config(config)
            self.refresh_runtime_workspace_config(config)

            self.log.log(
                f"Linked workspace version {key}: {folder}"
            )

            refresh_manager()

            self.reload_workspace(
                ACTIVE_MOD,
                queue_reconcile=True,
                reason="VERSION_LINK",
            )

        def repoint_selected_version():

            key = get_selected_version_key()

            if not key:
                return

            add_or_link_workspace_root(
                version_key=key,
            )

        def use_selected_version():

            key = get_selected_version_key()

            if not key:
                return

            if not selected_version_is_usable(
                key,
                notify=True,
            ):
                return

            config = load_config()

            versions = self.ensure_workspace_version_entries(
                ACTIVE_MOD,
                config,
            )

            entry = versions.get(key)

            if isinstance(entry, str):
                entry = {"path": entry}

            config.setdefault(
                "active_workspace_versions",
                {},
            )[ACTIVE_MOD] = key

            save_config(config)
            self.refresh_runtime_workspace_config(config)

            refresh_manager()

            self.reload_workspace(
                ACTIVE_MOD,
                queue_reconcile=True,
                reason="VERSION_SWITCH",
            )

        def remove_selected_link():

            key = get_selected_version_key()

            if not key:
                return

            config = load_config()

            versions = self.ensure_workspace_version_entries(
                ACTIVE_MOD,
                config,
            )

            if key not in versions:
                return

            remove = messagebox.askyesno(
                "Remove Linked Workspace Root",
                (
                    f"Remove the linked workspace root for {key}?\n\n"
                    "This does not delete patch files."
                ),
                parent=editor,
            )

            if not remove:
                return

            versions.pop(key, None)

            active_versions = config.setdefault(
                "active_workspace_versions",
                {},
            )

            if active_versions.get(ACTIVE_MOD) == key:
                active_versions.pop(ACTIVE_MOD, None)

            save_config(config)
            self.refresh_runtime_workspace_config(config)

            refresh_manager()

            if key == self.get_active_version_label():

                self.reload_workspace(
                    ACTIVE_MOD,
                    queue_reconcile=True,
                    reason="VERSION_UNLINK",
                )

        def open_selected_workspace_root():

            key = get_selected_version_key()

            if not key:
                return

            config = load_config()

            entry = get_workspace_version_entry(
                config,
                ACTIVE_MOD,
                key,
            )

            if entry and entry.get("path"):
                self.open_path(Path(entry["path"]))

        def open_selected_patch_folder():

            key = get_selected_version_key()

            if not key:
                return

            self.open_path(
                CUSTOM_PATCH_ROOT /
                ACTIVE_MOD /
                key
            )

        def run_merge():

            source_key = source_var.get()
            target_key = target_var.get()

            if not source_key or not target_key:
                return

            proceed = messagebox.askyesno(
                "Merge Patch Folder",
                (
                    f"Copy all files from {source_key} into {target_key}?\n\n"
                    "Existing files will "
                    + (
                        "be overwritten."
                        if overwrite_var.get()
                        else "be kept."
                    )
                ),
                parent=editor,
            )

            if not proceed:
                return

            try:

                copied, skipped = self.merge_patch_folder(
                    source_key,
                    target_key,
                    overwrite=overwrite_var.get(),
                )

                self.log.log(
                    "Patch folder merge complete: "
                    f"{source_key} -> {target_key} "
                    f"({copied} copied, {skipped} skipped)"
                )

                messagebox.showinfo(
                    "Merge Complete",
                    (
                        f"Copied: {copied}\n"
                        f"Skipped: {skipped}"
                    ),
                    parent=editor,
                )

                refresh_manager()

                self.schedule_refresh(
                    scope=REFRESH_FULL
                )

            except Exception as e:

                messagebox.showerror(
                    "Merge Failed",
                    str(e),
                    parent=editor,
                )

        ttk.Button(
            controls,
            text="Add / Link Workspace Root",
            command=add_or_link_workspace_root,
        ).pack(
            side=tk.LEFT,
            padx=(0, 6),
        )

        ttk.Button(
            controls,
            text="Repoint Selected",
            command=repoint_selected_version,
        ).pack(
            side=tk.LEFT,
            padx=6,
        )

        use_button = ttk.Button(
            controls,
            text="Use Selected Version",
            command=use_selected_version,
        )

        use_button.pack(
            side=tk.LEFT,
            padx=6,
        )

        ttk.Button(
            controls,
            text="Remove Link",
            command=remove_selected_link,
        ).pack(
            side=tk.LEFT,
            padx=6,
        )

        ttk.Button(
            controls,
            text="Open Workspace",
            command=open_selected_workspace_root,
        ).pack(
            side=tk.LEFT,
            padx=6,
        )

        ttk.Button(
            controls,
            text="Open Patch Folder",
            command=open_selected_patch_folder,
        ).pack(
            side=tk.LEFT,
            padx=6,
        )

        ttk.Button(
            merge_body,
            text="Merge Patch Folder",
            command=run_merge,
        ).grid(
            row=1,
            column=3,
            sticky="e",
            padx=4,
            pady=4,
        )

        bottom = ttk.Frame(editor)

        bottom.pack(
            fill=tk.X,
            padx=12,
            pady=(0, 12),
        )

        ttk.Button(
            bottom,
            text="Refresh",
            command=refresh_manager,
        ).pack(
            side=tk.LEFT,
        )

        ttk.Button(
            bottom,
            text="Close",
            command=editor.destroy,
        ).pack(
            side=tk.RIGHT,
        )

        version_tree.bind(
            "<Double-1>",
            lambda _event: use_selected_version(),
        )

        version_tree.bind(
            "<<TreeviewSelect>>",
            update_version_button_state,
        )

        refresh_manager()
		            
		    
    def change_patch_root(self):
    
        global CUSTOM_PATCH_ROOT
        global CONFIG
    
        selected = filedialog.askdirectory(
            title="Select Patch Root"
        )
    
        if not selected:
            return
    
        CONFIG = load_config()
    
        CUSTOM_PATCH_ROOT = Path(selected)
    
        CONFIG["patch_root"] = selected
    
        save_config(CONFIG)
    
        self.log.log(
            f"Patch root changed: {selected}"
        )
    
        if ACTIVE_MOD and ACTIVE_MOD in MODS:
    
            self.reload_workspace(
                ACTIVE_MOD,
            )
    
        else:
    
            self.show_no_workspace_loaded()


    def validate_workspace_folder(
        self,
        folder,
    ):

        path = Path(folder)

        issues = []

        if not path.exists():

            issues.append(
                "Selected folder does not exist."
            )

            return {
                "valid": False,
                "issues": issues,
                "generator": "unknown",
                "mc_version": "UNKNOWN",
            }

        if not path.is_dir():

            issues.append(
                "Selected path is not a folder."
            )

            return {
                "valid": False,
                "issues": issues,
                "generator": "unknown",
                "mc_version": "UNKNOWN",
            }

        build_gradle = path / "build.gradle"

        identity = detect_workspace_identity(path)

        generator = identity["generator"]

        mc_version = identity["mc_version"]

        if not build_gradle.exists():

            issues.append(
                "build.gradle was not found."
            )

        if generator == "unknown":

            issues.append(
                "A supported generator could not be detected "
                "(Forge, NeoForge, Fabric, or Quilt)."
            )

        if mc_version == "UNKNOWN":

            issues.append(
                "Minecraft version could not be detected."
            )

        if not (path / "src" / "main" / "java").exists():

            issues.append(
                "src/main/java was not found."
            )

        return {
            "valid": not issues,
            "issues": issues,
            "generator": generator,
            "mc_version": mc_version,
        }


    def confirm_workspace_folder(
        self,
        folder,
    ):

        validation = self.validate_workspace_folder(
            folder
        )

        if validation["valid"]:
            return True

        issues = "\n".join(
            f"- {issue}"
            for issue in validation["issues"]
        )

        generator = validation["generator"]

        if generator == "unknown":
            generator = "not detected"

        mc_version = validation["mc_version"]

        if mc_version == "UNKNOWN":
            mc_version = "not detected"

        return messagebox.askyesno(
            "Workspace May Be Invalid",
            (
                "This folder does not look like a supported "
                "MCreator workspace.\n\n"
                f"{issues}\n\n"
                f"Detected generator: {generator}\n"
                f"Detected Minecraft version: {mc_version}\n\n"
                "If you add it anyway, MCodePatcher may create "
                "patch and backup folders for a workspace that "
                "cannot be patched correctly.\n\n"
                "Add this folder anyway?"
            ),
            icon=messagebox.WARNING,
            default=messagebox.NO,
        )
    
    
    def add_workspace(self):
    
        global CONFIG
        global MODS
    
        CONFIG = load_config()
    
        name = simpledialog.askstring(
            "Workspace Name",
            "Enter workspace name:",
        )
    
        if not name:
            return
    
        name = name.strip()
    
        name = re.sub(
            r"[^a-zA-Z0-9_\-]",
            "_",
            name,
        )
    
        if not name:
            return
    
        folder = filedialog.askdirectory(
            title="Select Workspace Folder"
        )
    
        if not folder:
            return

        if not self.confirm_workspace_folder(
            folder
        ):
            return
    
        CONFIG.setdefault("mods", {})[name] = folder

        identity = detect_workspace_identity(
            Path(folder),
            log_callback=self.log.log,
        )

        version_key = identity["generator_folder"]

        CONFIG.setdefault(
            "workspace_versions",
            {},
        ).setdefault(name, {})[
            version_key
        ] = {
            "path": folder,
            "generator": identity["generator"],
            "mc_version": identity["mc_version"],
            "label": version_key,
            "updated": time.time(),
        }

        CONFIG.setdefault(
            "active_workspace_versions",
            {},
        )[name] = version_key
    
        save_config(CONFIG)
    
        MODS = build_mod_map(CONFIG)
    
        self.workspace_var.set(name)
    
        self.refresh_workspace_menu()
    
        self.reload_workspace(name)
        self.refresh_toggle_button_state()
        
    def create_injection(self):
    
        if not self.require_workspace():
            return
        name = simpledialog.askstring(
            "Create Snippet Injection",
            (
                "Snippet injections replace matching "
                "markers with reusable shared code.\n\n"
        
                "Useful for:\n"
                "• shared procedures\n"
                "• code fixes\n"
                "• repeated logic\n"
                "• reusable generated fragments\n\n"
        
                "Example name:\n\n"
                "codefixes\n\n"
        
                "This creates:\n"
                "codefixes.txt\n"
                "inside the Snippet Injections folder.\n\n"
        
                "Use this marker inside MCreator:\n\n"
                "// MSNIPPET:codefixes\n\n"
        
                "When patched, matching markers "
                "will be replaced with the contents "
                "of this file.\n\n"
        
                "Snippet injections are version-specific "
                "and remain persistent across "
                "regenerations and generator switches."
            ),
        )
    
        if not name:
            return
    
        # =========================================
        # SANITISE
        # =========================================
    
        name = name.strip()
    
        name = re.sub(
            r"[^a-zA-Z0-9_\-]",
            "_",
            name,
        )
    
        if not name:
            return
    
        injection_path = (
            self.engine.injection_root /
            f"{name}.txt"
        )
    
        if injection_path.exists():
    
            messagebox.showinfo(
                "Injection Exists",
                (
                    "That injection already exists."
                ),
            )
    
            return
    
        try:
    
            injection_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
    
            injection_path.write_text(
                "",
                encoding="utf-8",
            )
    
            self.log.log(
                f"Created injection: {name}"
            )
    
            self.schedule_refresh(
                scope=REFRESH_FULL
            )
    
            # =====================================
            # SELECT NEW FILE
            # =====================================
    
            self.select_path_in_tree(
                self.patch_tree,
                injection_path,
            )
    
            # =====================================
            # AUTO OPEN
            # =====================================
    
            self.open_selected_file(
                self.patch_tree
            )
    
            # =====================================
            # HELP
            # =====================================
    
            messagebox.showinfo(
                "Injection Created",
                (
                    f"Injection file created:\n\n"
                    f"{name}.txt\n\n"
                    f"Use this inside MCreator:\n\n"
                    f"// MSNIPPET:{name}"
                ),
            )
    
        except Exception as e:
    
            messagebox.showerror(
                "Injection Failed",
                str(e),
            )
    
    def create_header(self):
    
        if not self.require_workspace():
            return
        name = simpledialog.askstring(
            "Create Header Injection",
            (
                "Header injections replace everything ABOVE "
                "a marker with shared reusable code.\n\n"
        
                "Useful for:\n"
                "• imports\n"
                "• helper methods\n"
                "• shared constants\n"
                "• generator-specific setup\n\n"
        
                "Example name:\n\n"
                "entity_imports\n\n"
        
                "This creates:\n"
                "entity_imports.txt\n"
                "inside the Header Injections folder.\n\n"
        
                "Use this marker inside MCreator:\n\n"
                "// MHEADER:entity_imports\n\n"
        
                "When patched, everything ABOVE "
                "the marker will be replaced with "
                "the contents of this file.\n\n"
        
                "Best used near the top of generated files.\n\n"
        
                "Header injections are version-specific "
                "and remain persistent across "
                "regenerations and generator switches."
            ),
        )
    
        if not name:
            return
    
        # =========================================
        # SANITISE
        # =========================================
    
        name = name.strip()
    
        name = re.sub(
            r"[^a-zA-Z0-9_\-]",
            "_",
            name,
        )
    
        if not name:
            return
    
        header_path = (
            self.engine.header_root /
            f"{name}.txt"
        )
    
        if header_path.exists():
    
            messagebox.showinfo(
                "Header Exists",
                (
                    "That header already exists."
                ),
            )
    
            return
    
        try:
    
            header_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
    
            header_path.write_text(
                "",
                encoding="utf-8",
            )
    
            self.log.log(
                f"Created header: {name}"
            )
    
            self.schedule_refresh(
                scope=REFRESH_FULL
            )
    
            # =====================================
            # SELECT NEW FILE
            # =====================================
    
            self.select_path_in_tree(
                self.patch_tree,
                header_path,
            )
    
            # =====================================
            # AUTO OPEN
            # =====================================
    
            self.open_selected_file(
                self.patch_tree
            )
    
            # =====================================
            # HELP
            # =====================================
    
            messagebox.showinfo(
                "Header Created",
                (
                    f"Header file created:\n\n"
                    f"{name}.txt\n\n"
                    f"Use this inside MCreator:\n\n"
                    f"// MHEADER:{name}\n\n"
                    f"Everything ABOVE the marker\n"
                    f"will be replaced with the\n"
                    f"contents of this file, when the generator matches."
                ),
            )
    
        except Exception as e:
    
            messagebox.showerror(
                "Header Failed",
                str(e),
            )
    
    def create_advanced_injection(self):
    
        if not self.require_workspace():
            return
        name = simpledialog.askstring(
            "Create Advanced Injection Rule",
            (
                "Advanced Injection Rules allow dynamic "
                "code replacement across generated files.\n\n"
            
                "Unlike snippet overrides, these operate "
                "by searching for matching code patterns "
                "inside generated files and replacing them.\n\n"
            
                "Useful for:\n"
                "• generator-wide fixes\n"
                "• replacing broken generated code\n"
                "• mass injections\n"
                "• patching imports\n"
                "• replacing AI-generated fragments\n\n"
            
                "Scopes:\n"
                "• single file (default and safest)\n"
                "• entire directory\n"
                "• multiple targets via the editor\n"
                "• directory targets include all eligible files inside them\n\n"
            
                "Actions:\n"
                "• replace_first\n"
                "• replace_all\n\n"
            
                "Replacement sources:\n"
                "• inline replacement text\n"
                "• snippet override files\n\n"

                "After creation, the built-in editor opens "
                "so you can fill in the rule without touching JSON. "
                "You can edit it later by double-clicking it in "
                "Advanced Rule Usage History.\n\n"
            
                "Rules are version-specific and persist "
                "across regenerations."
            ),
        )
    
        if not name:
            return
    
        name = name.strip()
    
        name = re.sub(
            r"[^a-zA-Z0-9_\-]",
            "_",
            name,
        )
    
        if not name:
            return
    
        rule_path = (
            self.engine.advanced_injection_root /
            f"{name}.inject.json"
        )
    
        if rule_path.exists():
    
            messagebox.showinfo(
                "Exists",
                "That rule already exists.",
            )
    
            return
    
        example = self.make_default_advanced_rule(
            name
        )
    
        try:
    
            rule_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
    
            rule_path.write_text(
                json.dumps(
                    example,
                    indent=4,
                ),
                encoding="utf-8",
            )
    
            self.schedule_refresh(
                scope=REFRESH_FULL
            )
    
            self.select_path_in_tree(
                self.patch_tree,
                rule_path,
            )

            self.open_advanced_rule_editor(
                rule_path,
                is_new=True,
            )
    
        except Exception as e:
    
            messagebox.showerror(
                "Creation Failed",
                str(e),
            )
    
    def select_path_in_tree(
        self,
        tree,
        target_path,
        additive=True,
    ):
    
        found = False
    
        for node in tree.get_children():
    
            if self.reselect_recursive(
                tree,
                node,
                target_path,
                additive=additive,
            ):
                found = True
            
                if not additive:
                    break

        
        if found:
    
            selection = tree.selection()
    
            if selection:
    
                focus = selection[-1]
    
                values = tree.item(
                    focus,
                    "values",
                )
    
                if values:
    
                    try:
    
                        path = Path(values[-1])
    
                        self.update_toggle_button(path)
                        self.update_status(path)
    
                    except Exception:
                        pass
    
        return found
    
    def create_override_from_workspace(self):
    
        if not self.require_workspace():
            return
        path = self.get_selected_path(
            self.workspace_tree
        )
    
        if not path:
            return
    
        if not path.is_file():
            return
    
        patch_path = (
            self.engine.to_patch_path(path)
        )
    
        if not patch_path:
            return
    
        # =========================================
        # ALREADY EXISTS
        # =========================================
    
        if patch_path.exists():
    
            result = messagebox.askyesno(
                "Override Exists",
                (
                    "Patch override already exists.\n\n"
                    "Overwrite it with the workspace version?"
                ),
            )
    
            if not result:
                return
    
        try:
    
            patch_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
    
            shutil.copy2(
                path,
                patch_path,
            )
    
            self.log.log(
                (
                    "Created override: "
                    f"{patch_path.name}"
                )
            )
    
            self.schedule_refresh(
                scope=REFRESH_FULL
            )
    
            # =====================================
            # AUTO-SELECT NEW PATCH FILE
            # =====================================
    
            self.select_path_in_tree(
                self.patch_tree,
                patch_path,
            )
    
        except Exception as e:
    
            messagebox.showerror(
                "Override Failed",
                str(e),
            )
    def build_reference_model(self):
    
        if not self.engine:
    
            return {
    
                "files": [],
    
                "snippets": [],
    
                "headers": [],
    
                "advanced": [],
            }
    
        model = {
    
            "files": [],
    
            "snippets": [],
    
            "headers": [],
    
            "advanced": [],
        }
    
        # =====================================================
        # FILE OVERRIDES
        # =====================================================
    
        try:
    
            patch_files = (
                self.engine.scan_patch_files()
            )
    
            for entry in patch_files:
    
                model["files"].append({
    
                    "path":
                        entry["path"],
    
                    "exists":
                        entry["exists"],
    
                    "modified":
                        entry["modified"],
    
                    "disabled":
                        entry["disabled"],
                })
    
        except Exception as e:
    
            self.log.log(
                f"Reference file scan failed: {e}"
            )
    
        # =====================================================
        # PRELOAD SNIPPET FILES
        # =====================================================
    
        snippet_files = {}
    
        try:
    
            if self.engine.injection_root.exists():
    
                for file in (
                    self.engine.injection_root.glob(
                        "*.txt*"
                    )
                ):
    
                    marker = (
                        self.engine.normalize_marker_name(
                            self.engine
                            .normalized_disabled_path(
                                file
                            ).stem
                        )
                    )
    
                    snippet_files[
                        marker
                    ] = {
    
                        "path": file,
    
                        "disabled":
                            self.engine.is_disabled(
                                file
                            ),
                    }
    
        except Exception as e:
    
            self.log.log(
                f"Snippet preload failed: {e}"
            )
    
        # =====================================================
        # PRELOAD HEADER FILES
        # =====================================================
    
        header_files = {}
    
        try:
    
            if self.engine.header_root.exists():
    
                for file in (
                    self.engine.header_root.glob(
                        "*.txt*"
                    )
                ):
    
                    marker = (
                        self.engine.normalize_marker_name(
                            self.engine
                            .normalized_disabled_path(
                                file
                            ).stem
                        )
                    )
    
                    header_files[
                        marker
                    ] = {
    
                        "path": file,
    
                        "disabled":
                            self.engine.is_disabled(
                                file
                            ),
                    }
    
        except Exception as e:
    
            self.log.log(
                f"Header preload failed: {e}"
            )
    
        # =====================================================
        # WORKSPACE SCAN
        # =====================================================
    
        seen_snippets = set()
        seen_builtin_snippets = set()
        seen_headers = set()

        seen_snippet_markers = set()
        seen_header_markers = set()

        snippet_reference_index = defaultdict(set)
        header_reference_index = defaultdict(set)
    
        try:
    
            for workspace_file in (
                self.engine.iter_workspace_files()
            ):
    
                if not workspace_file.is_file():
                    continue
    
                try:
    
                    content = (
                        workspace_file.read_text(
                            encoding="utf-8",
                            errors="ignore",
                        )
                    )
    
                except Exception:
                    continue
    
                try:
    
                    relative_target = str(
    
                        workspace_file.relative_to(
                            self.engine.workspace_root
                        )
    
                    ).replace("\\", "/")
    
                except Exception:
    
                    relative_target = (
                        workspace_file.name
                    )
    
                lines = content.splitlines()
    
                # =============================================
                # SNIPPETS
                # =============================================
    
                for line in lines:

                    marker_details = (
                        self.engine.extract_marker_details_from_line(
                            line,
                            "snippet",
                            include_applied=True,
                        )
                    )
    
                    if not marker_details:
                        continue
    
                    applied = marker_details[
                        "applied"
                    ]
    
                    marker = marker_details[
                        "marker"
                    ]

                    seen_snippet_markers.add(marker)

                    snippet_reference_index[
                        marker
                    ].add(workspace_file)
    
                    dedupe_key = (
    
                        marker,
    
                        relative_target,
    
                        applied,
                    )
    
                    if dedupe_key in seen_snippets:
                        continue
    
                    seen_snippets.add(
                        dedupe_key
                    )
    
                    source_data = (
                        snippet_files.get(marker)
                    )
    
                    model["snippets"].append({
    
                        "marker":
                            marker,

                        "operation":
                            "snippet",
    
                        "workspace_file":
                            workspace_file,
    
                        "relative":
                            relative_target,
    
                        "applied":
                            applied,
    
                        "source_file":
    
                            source_data["path"]
                            if source_data
                            else None,
    
                        "disabled":

                            source_data["disabled"]
                            if source_data
                            else False,

                        "source_only":
                            False,
                    })

                # =============================================
                # INLINE IMPORTS
                # =============================================

                for line_number, line in enumerate(
                    lines,
                    1,
                ):

                    import_details = (
                        self.engine
                        .extract_import_details_from_line(
                            line,
                            include_applied=True,
                        )
                    )

                    if not import_details:
                        continue

                    import_statement = (
                        import_details["import"]
                    )

                    applied = import_details[
                        "applied"
                    ]

                    dedupe_key = (
                        "import",
                        import_statement,
                        relative_target,
                        applied,
                    )

                    if dedupe_key in seen_builtin_snippets:
                        continue

                    seen_builtin_snippets.add(
                        dedupe_key
                    )

                    model["snippets"].append({

                        "marker":
                            import_statement,

                        "operation":
                            "import",

                        "workspace_file":
                            workspace_file,

                        "relative":
                            relative_target,

                        "applied":
                            applied,

                        "source_file":
                            None,

                        "disabled":
                            False,

                        "source_only":
                            False,

                        "display":
                            import_statement,

                        "line":
                            line_number,
                    })

                # =============================================
                # INLINE REMOVALS
                # =============================================

                removal_count = 0

                for line_number, line in enumerate(
                    lines,
                    1,
                ):

                    remove_details = (
                        self.engine
                        .extract_remove_details_from_line(
                            line,
                            include_applied=True,
                        )
                    )

                    if not remove_details:
                        continue

                    if remove_details["kind"] == "end":
                        continue

                    removal_count += 1

                    applied = remove_details[
                        "applied"
                    ]

                    display = (
                        f"Removal block {removal_count}"
                    )

                    dedupe_key = (
                        "removal",
                        relative_target,
                        line_number,
                        applied,
                    )

                    if dedupe_key in seen_builtin_snippets:
                        continue

                    seen_builtin_snippets.add(
                        dedupe_key
                    )

                    model["snippets"].append({

                        "marker":
                            "__mremove__",

                        "operation":
                            "removal",

                        "workspace_file":
                            workspace_file,

                        "relative":
                            relative_target,

                        "applied":
                            applied,

                        "source_file":
                            None,

                        "disabled":
                            False,

                        "source_only":
                            False,

                        "display":
                            display,

                        "line":
                            line_number,
                    })
    
                # =============================================
                # HEADERS
                # =============================================
    
                for line in lines:

                    marker_details = (
                        self.engine.extract_marker_details_from_line(
                            line,
                            "header",
                            include_applied=True,
                        )
                    )
    
                    if not marker_details:
                        continue
    
                    applied = marker_details[
                        "applied"
                    ]
    
                    marker = marker_details[
                        "marker"
                    ]

                    seen_header_markers.add(marker)

                    header_reference_index[
                        marker
                    ].add(workspace_file)
    
                    dedupe_key = (
    
                        marker,
    
                        relative_target,
    
                        applied,
                    )
    
                    if dedupe_key in seen_headers:
                        continue
    
                    seen_headers.add(
                        dedupe_key
                    )
    
                    source_data = (
                        header_files.get(marker)
                    )
    
                    model["headers"].append({
    
                        "marker":
                            marker,
    
                        "workspace_file":
                            workspace_file,
    
                        "relative":
                            relative_target,
    
                        "applied":
                            applied,
    
                        "source_file":
    
                            source_data["path"]
                            if source_data
                            else None,
    
                        "disabled":

                            source_data["disabled"]
                            if source_data
                            else False,

                        "source_only":
                            False,
                    })
    
        except Exception as e:
    
            self.log.log(
                f"Workspace reference scan failed: {e}"
            )

        self.engine.snippet_reference_index = (
            snippet_reference_index
        )

        self.engine.header_reference_index = (
            header_reference_index
        )

        # =====================================================
        # UNUSED SOURCE FILES
        # =====================================================

        for marker, source_data in sorted(
            snippet_files.items()
        ):

            if marker in seen_snippet_markers:
                continue

            model["snippets"].append({

                "marker":
                    marker,

                "operation":
                    "snippet",

                "workspace_file":
                    None,

                "relative":
                    "",

                "applied":
                    False,

                "source_file":
                    source_data["path"],

                "disabled":
                    source_data["disabled"],

                "source_only":
                    True,
            })

        for marker, source_data in sorted(
            header_files.items()
        ):

            if marker in seen_header_markers:
                continue

            model["headers"].append({

                "marker":
                    marker,

                "workspace_file":
                    None,

                "relative":
                    "",

                "applied":
                    False,

                "source_file":
                    source_data["path"],

                "disabled":
                    source_data["disabled"],

                "source_only":
                    True,
            })
    
        # =====================================================
        # ADVANCED RULE HISTORY
        # =====================================================
    
        try:

            advanced_rules = (
                self.engine.scan_advanced_rules()
            )

            for rule in advanced_rules:

                rule_path = rule.get("path")

                if not rule_path:
                    continue

                model["advanced"].append({

                    "rule":
                        Path(rule_path).stem,

                    "rule_file":
                        str(rule_path),

                    "target":
                        "",

                    "relative_target":
                        "",

                    "timestamp":
                        0,

                    "status":
                        (
                            "UNUSED"
                            if rule.get("enabled", False)
                            else "DISABLED"
                        ),

                    "enabled":
                        rule.get("enabled", False),

                    "scope":
                        rule.get("scope", "invalid target"),

                    "regex":
                        rule.get("regex", False),

                    "type":
                        rule.get("type", "replace_all"),

                    "source_only":
                        True,
                })
    
            advanced_history = (
                self.engine
                .load_advanced_reference_history()
            )
    
            for entry in advanced_history:
    
                rule_file = entry.get(
                    "rule_file",
                    "",
                )
    
                target = Path(
                    entry.get(
                        "target",
                        "",
                    )
                )
    
                rule_hash = entry.get(
                    "rule_hash",
                    "",
                )
    
                target_hash = entry.get(
                    "content_hash_after",
                    "",
                )
    
                status = "VALID"
    
                try:
    
                    rule_path = Path(rule_file)
    
                    if not target.exists():
    
                        status = "MISSING"
    
                    else:
    
                        current_target_hash = (
                            self.engine.compute_file_hash(
                                target
                            )
                        )
    
                        current_rule_hash = None
    
                        if rule_path.exists():
    
                            current_rule_hash = (
                                self.engine.compute_file_hash(
                                    rule_path
                                )
                            )
    
                        if (
                            current_rule_hash
                            != rule_hash
                        ):
    
                            status = "RULE CHANGED"
    
                        elif (
                            current_target_hash
                            != target_hash
                        ):
    
                            status = "STALE"
    
                except Exception:
    
                    status = "ERROR"
    
                entry["status"] = status

                entry["source_only"] = False
    
                model["advanced"].append(
                    entry
                )
    
        except Exception as e:
    
            self.log.log(
                f"Advanced reference scan failed: {e}"
            )
    
        return model
    
                    
    def on_workspace_search_changed(self, *_):
    
        if self.is_refreshing_trees:
            return
    
        if self.workspace_search_after:
    
            try:
                self.root.after_cancel(
                    self.workspace_search_after
                )
            except Exception:
                pass
    
        self.workspace_search_after = self.root.after(
            250,
            self.run_workspace_search_refresh,
        )
    
    def run_workspace_search_refresh(self):
    
        self.workspace_search_after = None
    
        if self.is_refreshing_trees:
            return
    
        self.refresh_workspace_tree_only()
    
    
    def run_patch_search_refresh(self):
    
        self.patch_search_after = None
    
        if self.is_refreshing_trees:
            return
    
        self.refresh_patch_tree_only()
        
    def on_patch_search_changed(self, *_):
    
        if self.is_refreshing_trees:
            return
    
        if self.patch_search_after:
    
            try:
                self.root.after_cancel(
                    self.patch_search_after
                )
            except Exception:
                pass
    
        self.patch_search_after = self.root.after(
            250,
            self.run_patch_search_refresh,
        )
        
    def refresh_workspace_tree_only(self):
    
        if not self.engine:
            return
    
        search = (
            self.workspace_search.get()
            .strip()
        )
    
        if search:
    
            self.tree_manager.populate_search_results(
                self.workspace_tree,
                self.engine.workspace_root,
                self.state.workspace_node_map,
                search,
            )
    
        else:
    
            self.tree_manager.populate(
                self.workspace_tree,
                self.engine.workspace_root,
                self.state.workspace_node_map,
                glow=True,
            )
    
    
    def refresh_patch_tree_only(self):
    
        if not self.engine:
            return
    
        search = (
            self.patch_search.get()
            .strip()
        )
    
        if search:
    
            self.tree_manager.populate_search_results(
                self.patch_tree,
                self.engine.patch_root,
                self.state.patch_node_map,
                search,
            )
    
        else:
    
            self.tree_manager.populate(
                self.patch_tree,
                self.engine.patch_root,
                self.state.patch_node_map,
            )
        
    def __init__(self):
    
        self.state = AppState()
    
        self.log = GuiLogger()
    
        # =========================================
        # ASYNC REFRESH SYSTEM
        # =========================================
        
        self.refresh_pending = False
        self.refresh_in_progress = False
        
        self.refresh_generation = 0
        self.pending_refresh_generation = 0
        
        self.refresh_after_id = None
        
        # =========================================
        # SCOPED REFRESH STATE
        # =========================================
        
        self.pending_refresh_scope = REFRESH_MINIMAL
        
        self.pending_changed_paths = set()
        
        self.background_executor = (
            concurrent.futures.ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="RefreshWorker",
            )
        )
    
        # UI apply queue
        self.ui_apply_queue = queue.Queue()
    
        
    
        self.engine = None
    
        self.workspace_search_after = None
        self.patch_search_after = None
        self.workspace_identity_after = None
    
        self.syncing_reference_selection = False
        self.selection_side_effect_suppression_active = False
        self.selection_side_effect_suppression_token = 0

        self.backup_panel_visible = False
        self.current_backup_target = None
        self.backup_entry_map = {}
    
        self.lock = threading.Lock()
    
        self.last_event_time = 0
    
        self.is_shutting_down = False
        self.loop_after_id = None
    
        self.observer = None
    
        self.root = tk.Tk()
    
        self.root.tk.call(
            "tk",
            "scaling",
            1.0,
        )
        
        # Loading state
        self.loading_var = tk.StringVar(
            value="",
        )
    
        self.workspace_search = tk.StringVar()
    
        self.patch_search = tk.StringVar()
    
        self.is_refreshing_trees = False
    
        self.activity_queue = queue.Queue()
    
        self.root.title(
            "MCodePatcher - No Workspace Loaded"
        )
    
        self.root.geometry("1400x900")
    
        self.root.protocol(
            "WM_DELETE_WINDOW",
            self.shutdown,
        )
    
        self.root.report_callback_exception = (
            self.tk_exception
        )
    
        self.tree_manager = TreeManager(self)
    
        self.setup_gui()
    
        if ACTIVE_MOD and ACTIVE_MOD in MODS:
    
            self.reload_workspace(ACTIVE_MOD)
    
        else:
    
            self.show_no_workspace_loaded()
    
        self.root.after(
            100,
            self.process_loop,
        )
    
        self.root.after(
            200,
            self.initialise_pane_positions,
        )
    
    def initialise_pane_positions(self):
    
        try:
    
            self.root.update_idletasks()
    
            total_width = (
                self.main_pane.winfo_width()
            )
    
            if total_width > 0:
    
                # =====================================
                # MAIN HORIZONTAL SPLIT
                # =====================================
    
                desired = max(
                    420,
                    min(
                        550,
                        total_width // 3,
                    ),
                )
    
                self.main_pane.sashpos(
                    0,
                    desired,
                )
    
            # =====================================
            # LEFT VERTICAL SPLIT
            # =====================================
    
            left_height = (
                self.left_vertical_pane.winfo_height()
            )
    
            if left_height > 0:
    
                self.left_vertical_pane.sashpos(
                    0,
                    left_height // 2,
                )
    
            # =====================================
            # RIGHT VERTICAL SPLIT
            # =====================================
    
            right_height = (
                self.right_vertical_pane.winfo_height()
            )
    
            if right_height > 0:
    
                self.right_vertical_pane.sashpos(
                    0,
                    int(right_height * 0.45),
                )
    
            # =====================================
            # REFERENCE SPLIT
            # =====================================
    
            ref_height = (
                self.reference_vertical_pane.winfo_height()
            )
    
            if ref_height > 0:
    
                self.reference_vertical_pane.sashpos(
                    0,
                    int(ref_height * 0.75),
                )
    
            # =====================================
            # APPLY CLAMPS
            # =====================================
    
            self.clamp_main_pane()
            self.clamp_left_vertical_pane()
            self.clamp_right_vertical_pane()
            self.clamp_reference_pane()
    
        except Exception:
            pass
    

    def setup_gui(self):

        style = ttk.Style()


        default_font = tkfont.nametofont("TkDefaultFont")
        
        bold_font = tkfont.Font(
            family=default_font.cget("family"),
            size=default_font.cget("size"),
            weight="bold",
        )
        
        mirrored_font = tkfont.Font(
            family=default_font.cget("family"),
            size=default_font.cget("size"),
            weight="bold",
        )
        
        # =====================================================
        # WORKSPACE STATE
        # =====================================================
        
        self.workspace_var = tk.StringVar(
            value=(
                f"Workspace Tree   [{ACTIVE_MOD}]"
                if ACTIVE_MOD
                else "Workspace Tree"
            )
        )

        self.main_pane = ttk.PanedWindow(
            self.root,
            orient=tk.HORIZONTAL,
        )
        
        button_font = tkfont.Font(
                            family=default_font.cget("family"),
                            size=10,
                            weight="bold",
                        )

        style.configure(
            "Snippet.TButton",
            foreground="#ffc26c",
            font=button_font,
            
        )
        
        style.configure(
            "Header.TButton",
            foreground="#ffc26c",
            font=button_font,
        )
        
        style.configure(
            "Advanced.TButton",
            foreground="#ff9361",
            font=button_font,
        )
        
        style.configure(
            "Override.TButton",
            foreground="#ffe066",
            font=button_font,
        )

        style.configure(
            "Treeview",
            rowheight=24,
        )  

        self.main_pane.pack(fill=tk.BOTH, expand=True)
        
        self.main_pane.bind(
            "<ButtonRelease-1>",
            self.clamp_main_pane,
        )

        self.left_vertical_pane = ttk.PanedWindow(
            self.main_pane,
            orient=tk.VERTICAL,
        )

        self.main_pane.add(
            self.left_vertical_pane,
            weight=1,
        )
        
        

        header_font = tkfont.Font(
            family=default_font.cget("family"),
            size=14,
            weight="bold",
        )
        
        workspace_outer = ttk.Frame(
            self.left_vertical_pane
        )
        
        workspace_header = ttk.Frame(
            workspace_outer
        )
        
        workspace_header.pack(
            fill=tk.X,
            padx=8,
            pady=(4, 0),
        )
        
        title_row = ttk.Frame(workspace_header)
        title_row.pack(side=tk.LEFT)
        
        workspace_title = ttk.Label(
            title_row,
            text="Workspace Tree",
            font=header_font,
        )
        
        workspace_title.pack(side=tk.LEFT)
        workspace_font = (
            "Segoe UI",
            8,
        )
        self.workspace_name_label = ttk.Label(
            title_row,
            text=(
                f"   [{ACTIVE_MOD[:32]}{'...' if len(ACTIVE_MOD) > 32 else ''}]"
                if ACTIVE_MOD
                else ""
            ),
            font=workspace_font,
            foreground="#7fdcff",
            padding=(0, 4, 0, 0),
        )
        
        self.workspace_name_label.pack(side=tk.LEFT)
        
        workspace_menu_button = ttk.Menubutton(
            workspace_header,
            text="Workspaces",
        )
        
        workspace_menu_button.pack(
            side=tk.RIGHT,
        )
        
        self.workspace_menu = tk.Menu(
            workspace_menu_button,
            tearoff=False,
        )
        
        
        workspace_menu_button.configure(
            menu=self.workspace_menu,
        )
        
        self.refresh_workspace_menu()
        
        workspace_frame = ttk.LabelFrame(
            workspace_outer,
            text="",
        )
        
        workspace_frame.pack(
            fill=tk.BOTH,
            expand=True,
            padx=5,
            pady=(0, 5),
        )
        
        patch_outer = ttk.Frame(
            self.left_vertical_pane
        )
        
        patch_title = ttk.Label(
            patch_outer,
            text="Patch Tree",
            font=header_font,
        )
        
        patch_title.pack(
            anchor="w",
            padx=8,
            pady=(4, 0),
        )
        
        patch_frame = ttk.LabelFrame(
            patch_outer,
            text="",
        )
        
        patch_frame.pack(
            fill=tk.BOTH,
            expand=True,
            padx=5,
            pady=(0, 5),
        )
        
        self.left_vertical_pane.add(
            workspace_outer,
            weight=1,
        )
        
        self.left_vertical_pane.add(
            patch_outer,
            weight=1,
        )
        
        self.left_vertical_pane.configure(
            width=550,
        )
        
        self.left_vertical_pane.bind(
            "<ButtonRelease-1>",
            self.clamp_left_vertical_pane,
        )

        # =====================================================
        # WORKSPACE TREE
        # =====================================================
        
        workspace_search_frame = ttk.Frame(
            workspace_frame
        )
        
        workspace_search_frame.pack(
            fill=tk.X,
            padx=5,
            pady=(5, 0),
        )
        
        ttk.Label(
            workspace_search_frame,
            text="🔍 Search workspace..."
        ).pack(anchor="w")
        
        workspace_search_entry = ttk.Entry(
            workspace_search_frame,
            textvariable=self.workspace_search,
            font=("TkDefaultFont", 11),
        )
        
        workspace_search_entry.pack(
            fill=tk.X,
            expand=True,
        )
        
        self.workspace_search.set("")
        
        workspace_container = ttk.Frame(
            workspace_frame
        )
        
        workspace_container.pack(
            fill=tk.BOTH,
            expand=True,
        )
        
        workspace_container.pack_propagate(False)
        
        self.workspace_tree = ttk.Treeview(
            workspace_container,
            show="tree",
        )
        
        self.workspace_tree.column(
            "#0",
            width=500,
        )
        
        workspace_scroll = ttk.Scrollbar(
            workspace_container,
            orient="vertical",
            command=self.workspace_tree.yview,
        )
        
        self.workspace_tree.configure(
            yscrollcommand=workspace_scroll.set
        )
        
        workspace_scroll.pack(
            side=tk.RIGHT,
            fill=tk.Y,
        )
        
        self.workspace_tree.pack(
            side=tk.LEFT,
            fill=tk.BOTH,
            expand=True,
        )
        
        workspace_buttons = ttk.Frame(
            workspace_frame
        )
        
        workspace_buttons.pack(
            fill=tk.X,
            padx=5,
            pady=5,
        )
        
        ttk.Button(
            workspace_buttons,
            text="Refresh",
            command=self.refresh_trees,
        ).pack(side=tk.LEFT, padx=2)
        
        ttk.Button(
            workspace_buttons,
            text="Open",
            command=lambda: self.open_selected_file(
                self.workspace_tree
            ),
        ).pack(side=tk.LEFT, padx=2)
        
        ttk.Button(
            workspace_buttons,
            text="Reveal",
            command=lambda: self.reveal_selected_file(
                self.workspace_tree
            ),
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            workspace_buttons,
            text="Create File Override",
            style="Override.TButton",
            command=self.create_override_from_workspace,
        ).pack(side=tk.LEFT, padx=2)
        
        # =====================================================
        # PATCH ASSET BUTTONS
        # =====================================================
        
        patch_asset_frame = ttk.Frame(
            patch_frame
        )
        
        patch_asset_frame.pack(
            fill=tk.X,
            padx=5,
            pady=(5, 0),
        )
        
        ttk.Button(
            patch_asset_frame,
            text="Create Snippet Override",
            style="Snippet.TButton",
            command=self.create_injection,
        ).pack(side=tk.LEFT, padx=(2, 6))
        
        ttk.Button(
            patch_asset_frame,
            text="Create Header Override",
            style="Header.TButton",
            command=self.create_header,
        ).pack(side=tk.LEFT, padx=2)
        # =====================================================
        # SEARCH ROW
        # =====================================================
        
        patch_search_frame = ttk.Frame(
            patch_frame
        )
        
        patch_search_frame.pack(
            fill=tk.X,
            padx=5,
            pady=(0, 0),
        )
        
        # =========================================
        # TOP ROW
        # =========================================
        
        search_top_row = ttk.Frame(
            patch_search_frame
        )
        
        search_top_row.pack(
            fill=tk.X,
        )
        
        ttk.Label(
            search_top_row,
            text="🔍 Search patches..."
        ).pack(
            side=tk.LEFT,
            anchor="w",
        )
        
        ttk.Button(
            search_top_row,
            text="Create Advanced Injection Rule",
            style="Advanced.TButton",
            command=self.create_advanced_injection,
        ).pack(
            side=tk.LEFT,
            padx=(20, 4),
        )
        
        # =========================================
        # SEARCH ENTRY
        # =========================================
        
        patch_search_entry = ttk.Entry(
            patch_search_frame,
            textvariable=self.patch_search,
            font=("TkDefaultFont", 11),
        )
        
        patch_search_entry.pack(
            fill=tk.X,
            expand=True,
            pady=(4, 0),
        )
        
        self.patch_search.set("")
        
        patch_container = ttk.Frame(
            patch_frame
        )
        
        patch_container.pack(
            fill=tk.BOTH,
            expand=True,
        )
        patch_container.pack_propagate(False)
        
        self.patch_tree = ttk.Treeview(
        
            patch_container,
        
            show="tree",
            
            selectmode="extended",
        
        )
        
        self.patch_tree.column(
            "#0",
            width=500,
        )
        
        patch_scroll = ttk.Scrollbar(
            patch_container,
            orient="vertical",
            command=self.patch_tree.yview,
        )
        
        self.patch_tree.configure(
            yscrollcommand=patch_scroll.set
        )
        self.patch_tree.tag_configure(
                    "disabled",
                    foreground="#F76D23",
                )
        self.patch_tree.tag_configure(
            "injection_folder",
            foreground="#ffb347",
        )
        self.patch_tree.tag_configure(
            "active_version",
            foreground="#45ff83",
            font=bold_font,
        )
        self.patch_tree.tag_configure(
            "advanced_injection_folder",
            foreground="#ff5c1c",
        )
        
        self.patch_tree.tag_configure(
            "separator",
            foreground="#666666",
        )
        
        patch_scroll.pack(
            side=tk.RIGHT,
            fill=tk.Y,
        )
        
        self.patch_tree.pack(
            side=tk.LEFT,
            fill=tk.BOTH,
            expand=True,
        )
        
        patch_buttons = ttk.Frame(
            patch_frame
        )
        
        patch_buttons.pack(
            fill=tk.X,
            padx=5,
            pady=5,
        )
        
        ttk.Button(
            patch_buttons,
            text="Refresh",
            command=self.refresh_trees,
        ).pack(side=tk.LEFT, padx=2)
        
        ttk.Button(
            patch_buttons,
            text="Open",
            command=lambda: self.open_selected_file(
                self.patch_tree
            ),
        ).pack(side=tk.LEFT, padx=2)
        
        ttk.Button(
            patch_buttons,
            text="Reveal",
            command=lambda: self.reveal_selected_file(
                self.patch_tree
            ),
        ).pack(side=tk.LEFT, padx=2)
        
        self.toggle_button = ttk.Button(
            patch_buttons,
            text="Disable",
            command=lambda: self.toggle_selected_file(
                self.patch_tree
            ),
        )
        
        self.toggle_button.pack(
            side=tk.LEFT,
            padx=2,
        )

        self.workspace_tree.bind(
            "<<TreeviewSelect>>",
            self.tree_manager.on_select,
        )

        self.patch_tree.bind(
            "<<TreeviewSelect>>",
            self.tree_manager.on_select,
        )

        self.workspace_tree.bind(
            "<<TreeviewOpen>>",
            lambda e: self.tree_manager.sync_expand(
                e.widget,
                True,
            ),
        )

        self.workspace_tree.bind(
            "<<TreeviewClose>>",
            lambda e: self.tree_manager.sync_expand(
                e.widget,
                False,
            ),
        )

        self.patch_tree.bind(
            "<<TreeviewOpen>>",
            lambda e: self.tree_manager.sync_expand(
                e.widget,
                True,
            ),
        )

        self.patch_tree.bind(
            "<<TreeviewClose>>",
            lambda e: self.tree_manager.sync_expand(
                e.widget,
                False,
            ),
        )

        self.workspace_tree.tag_configure(
            "modified",
            foreground="#45ff83",
            font=bold_font,
        )
        
        self.workspace_tree.tag_configure(
            "mirrored",
            foreground="#7fdcff",
            font=mirrored_font,
        )
        
        self.workspace_tree.tag_configure(
            "marker_valid",
            foreground="#45ff83",
            font=bold_font,
        )
        
        self.workspace_tree.tag_configure(
            "marker_missing",
            foreground="#ff5c5c",
            font=bold_font,
        )

        self.right_vertical_pane = ttk.PanedWindow(
            self.main_pane,
            orient=tk.VERTICAL,
        )

        self.main_pane.add(
            self.right_vertical_pane,
            weight=3,
        )

        upper_right = ttk.Frame(
            self.right_vertical_pane
        )

        self.reference_vertical_pane = ttk.PanedWindow(
            self.right_vertical_pane,
            orient=tk.VERTICAL,
        )

        self.right_vertical_pane.add(
            upper_right,
            weight=1,
        )
        
        self.right_vertical_pane.add(
            self.reference_vertical_pane,
            weight=1,
        )
        
        self.right_vertical_pane.bind(
            "<ButtonRelease-1>",
            self.clamp_right_vertical_pane,
        )

        # =====================================================
        # STATUS + BRANDING
        # =====================================================
        
        status_frame = ttk.LabelFrame(
            upper_right,
            text="Selection Info",
        )
        
        status_frame.pack(
            fill=tk.X,
            padx=5,
            pady=5,
        )
        
        self.loading_label = ttk.Label(
            status_frame,
            textvariable=self.loading_var,
            foreground="#7fdcff",
            font=("TkDefaultFont", 10, "bold"),
        )
        
        self.loading_label.pack(
            anchor="e",
            padx=10,
            pady=(0, 4),
        )
        
        status_container = ttk.Frame(
            status_frame
        )
        
        status_container.pack(
            fill=tk.BOTH,
            expand=True,
        )
        
        # =====================================================
        # LEFT STATUS PANEL
        # =====================================================
        
        status_left = ttk.Frame(
            status_container
        )
        
        status_left.pack(
            side=tk.LEFT,
            fill=tk.BOTH,
            expand=True,
            padx=(8, 0),
            pady=8,
        )
        
        status_left.configure(
            width=500,
            height=120,
        )
        
        status_left.pack_propagate(False)
        
        status_scroll_container = ttk.Frame(
            status_left
        )
        
        status_scroll_container.pack(
            fill=tk.BOTH,
            expand=True,
        )
        
        status_scrollbar = ttk.Scrollbar(
            status_scroll_container,
            orient="vertical",
        )
        
        status_scrollbar.pack(
            side=tk.RIGHT,
            fill=tk.Y,
        )
        
        self.status_text = tk.Text(
            status_scroll_container,
            wrap="word",
            height=6,
            yscrollcommand=status_scrollbar.set,
            relief="flat",
            borderwidth=0,
        )
        
        self.status_text.pack(
            side=tk.LEFT,
            fill=tk.BOTH,
            expand=True,
        )
        
        status_scrollbar.config(
            command=self.status_text.yview
        )
        
        self.status_text.insert(
            "1.0",
            "Nothing selected.",
        )
        
        self.status_text.configure(
            state="disabled",
        )
        
        # =====================================================
        # RIGHT BRAND PANEL
        # =====================================================
        
        brand_outer = tk.Frame(
            status_container,
            bg=self.root.cget("bg")
        )
        
        brand_outer.pack(
            side=tk.RIGHT,
            fill=tk.Y,
            expand=False,
            padx=10,
            pady=8,
        )
        
        brand_outer.configure(
            width=20,
        )
        
        
        self.brand_canvas = tk.Canvas(
            brand_outer,
            highlightthickness=0,
            bd=0,
            bg=self.root.cget("bg"),
            height=120,
            cursor="fleur",
        )
        
        self.brand_canvas.pack(
            fill=tk.BOTH,
            expand=True,
        )
        
        
        CARD_BG = "#242424"
        
        def draw_rounded_rect(
            canvas,
            x1,
            y1,
            x2,
            y2,
            radius=24,
            **kwargs,
        ):
        
            points = [
        
                x1 + radius, y1,
                x2 - radius, y1,
        
                x2, y1,
                x2, y1 + radius,
        
                x2, y2 - radius,
                x2, y2,
        
                x2 - radius, y2,
                x1 + radius, y2,
        
                x1, y2,
                x1, y2 - radius,
        
                x1, y1 + radius,
                x1, y1,
        
            ]
        
            return canvas.create_polygon(
                points,
                smooth=True,
                splinesteps=36,
                **kwargs,
            )
        
        def redraw_brand_panel(event=None):
        
            self.brand_canvas.delete("background")
        
            w = self.brand_canvas.winfo_width()
            h = self.brand_canvas.winfo_height()
        
            enabled = self.patching_enabled.get()
            
            bg = "#242424" if enabled else "#2b1f1f"
            outline = "#2f2f2f" if enabled else "#5a2a2a"
            
            draw_rounded_rect(
                self.brand_canvas,
                2,
                2,
                w - 2,
                h - 2,
                radius=26,
                fill=bg,
                outline=outline,
                width=1,
                tags="background",
            )
        
            self.brand_canvas.tag_lower(
                "background"
            )
        
        def position_brand_widgets(event=None):
        
            w = self.brand_canvas.winfo_width()
            h = self.brand_canvas.winfo_height()
        
            self.brand_canvas.coords(
                "brand_title",
                w / 2,
                h / 2 - 28,
            )
        
            self.brand_canvas.coords(
                "brand_subtitle",
                w / 2,
                h / 2 + 8,
            )
        
            offset = 46
            
            self.brand_canvas.coords(
                "brand_state",
                w / 2,
                h / 2 + offset,
            )
        
        position_brand_widgets()
        
        self.brand_canvas.bind(
            "<Configure>",
            lambda e: (
                redraw_brand_panel(e),
                position_brand_widgets(e),
            ),
        )
        self.brand_title = tk.Label(
            self.brand_canvas,
            text="MCodePatcher",
            bg=CARD_BG,
            fg="#ff5c5c",
            font=(
                "TkDefaultFont",
                28,
                "bold",
            ),
        )
        
        self.brand_subtitle = tk.Label(
            self.brand_canvas,
            text=(
                "Advanced Persistent Patch System\n"
                "for MCreator"
            ),
            bg=CARD_BG,
            fg="#ff9a9a",
            font=(
                "TkDefaultFont",
                10,
            ),
        )
        
        self.brand_state_label = tk.Label(
            self.brand_canvas,
            text="PATCHING DISABLED",
            bg=CARD_BG,
            fg="#ff5c5c",
            font=(
                "TkDefaultFont",
                16,
                "bold",
            ),
        )
        self.brand_canvas.create_window(
            0,
            0,
            anchor="center",
            window=self.brand_title,
            tags="brand_title",
        )
        
        self.brand_canvas.create_window(
            0,
            0,
            anchor="center",
            window=self.brand_subtitle,
            tags="brand_subtitle",
        )
        
        self.brand_canvas.create_window(
            0,
            0,
            anchor="center",
            window=self.brand_state_label,
            tags="brand_state",
        )
        
        # =====================================================
        # ABOUT DIALOG
        # =====================================================
        
        
        
        self.brand_canvas.bind(
            "<Button-1>",
            lambda e: self.toggle_patching_enabled(),
        )
        
        self.brand_title.bind(
            "<Button-1>",
            lambda e: self.toggle_patching_enabled(),
        )
        
        self.brand_subtitle.bind(
            "<Button-1>",
            lambda e: self.toggle_patching_enabled(),
        )
        
        self.brand_state_label.bind(
            "<Button-1>",
            lambda e: self.toggle_patching_enabled(),
        )
        # =====================================================
        # PATCHING ENABLED
        # =====================================================
        
        self.patching_enabled = tk.BooleanVar(
            value=False,
        )
        self.update_branding_state()
        self.root.after(
            1,
            lambda: (
                redraw_brand_panel(),
                position_brand_widgets(),
            ),
        )
        
        # =====================================================
        # BRAND HELP PANEL
        # =====================================================
        
        self.brand_help_frame = tk.Frame(
            self.root,
            bg="#181818",
            bd=0,
            relief="flat",
            highlightthickness=0,
        )
        
        # =====================================================
        # RIGHT KEEPALIVE / SCROLL STRIP
        # =====================================================
        
        FOCUS_ZONE_WIDTH = 100
        
        
        def pointer_inside_brand_keepalive_zone():
        
            if not self.brand_help_visible:
                return False
        
            try:
        
                px, py = self.root.winfo_pointerxy()
        
            except Exception:
        
                return False
        
            x1 = self.brand_help_frame.winfo_rootx()
            y1 = self.brand_help_frame.winfo_rooty()
        
            x2 = x1 + self.brand_help_frame.winfo_width()
            y2 = y1 + self.brand_help_frame.winfo_height()
        
            if not (
                x1 <= px <= x2
                and
                y1 <= py <= y2
            ):
                return False
        
            zone_x1 = x2 - FOCUS_ZONE_WIDTH + 2
        
            return px >= zone_x1
        # =====================================================
        # HELP TEXT
        # =====================================================
        
        self.brand_help_text = tk.Text(
            self.brand_help_frame,
        
            wrap="word",
        
            bg="#181818",
            fg="#d0d0d0",
        
            relief="flat",
            borderwidth=0,
        
            highlightthickness=0,
            highlightbackground="#181818",
            highlightcolor="#181818",
        
            insertwidth=0,
            takefocus=0,
        
            padx=18,
            pady=16,
        
            font=("TkDefaultFont", 11),
        )
        
        self.brand_help_text.configure(
            padx=18,
        )
        
        # IMPORTANT:
        # leave room for focus strip
        
        self.brand_help_text.place(
            x=0,
            y=0,
            relheight=1.0,
            relwidth=1.0,
        )
        
        # =====================================================
        # HELP CONTENT
        # =====================================================
        
        self.brand_help_text.insert(
            "1.0",
            (
                "MCodePatcher for MCreator\n\n"

                "GLOBAL CONTROLS\n"
                "• Click the logo panel to enable/disable patch application\n"
                "• When disabled, file watching and reference tracking continue\n"
                "• Eligible changes are queued and apply after re-enabling\n"
                "• Use the Workspace menu to add/switch workspaces or patch root\n\n"

                "WORKSPACE VERSION MANAGER\n"
                "• Open from Workspace > Manage Workspace Versions\n"
                "• Link different generator/version folders to different MCreator exports\n"
                "• Switch active linked roots inside one saved logical workspace\n"
                "• Repoint a version folder when a port uses a different workspace directory\n"
                "• Merge/copy patch files from one generator folder into another\n"
                "• Existing target patch files are kept unless overwrite is enabled\n\n"

                "MCodePatcher automatically generates per-generator folder trees mirroring your entire MCreator workspaces when detected open\n\n"
                
                "WORKSPACE TREE\n"
                "• Browse, search, open, and reveal generated workspace files\n"
                "• Create File Override copies the selected file into Patch Tree\n"
                "• Workspace edits can trigger overrides, snippets, headers, or rules\n\n"

                "PATCH TREE (mirrors workspace tree)\n"
                "• Patches are grouped by workspace, generator, and Minecraft version\n"
                "• Normal files mirror the workspace path and act as file overrides\n"
                "• Rename files/folders with .mpatch_disabled to disable them\n"
                "• Multi-select patch files to enable/disable them together\n\n"

                "SNIPPET INJECTIONS\n"
                "• Stored in snippet_injections/name.txt\n"
                "• Marker: // MSNIPPET:name\n"
                "• Applied marker: // MSNIPPET_APPLIED:name\n"
                "• Inserts reusable code at the marker location\n"
                "• Inline import marker: // MIMPORT import example.Type;\n"
                "• MIMPORT moves the import into the Java import block\n"
                "• Removal markers: // MREMOVE_START and // MREMOVE_END\n"
                "• MREMOVE deletes generated body chunks without external files\n"
                "• Use MREMOVE to keep MCreator-generated imports/header material\n"
                "  while replacing the body with snippets that need those imports\n"
                "• With Reference Tracking off, raw markers are removed after patching\n\n"

                "HEADER INJECTIONS\n"
                "• Stored in header_injections/name.txt\n"
                "• Marker: // MHEADER:name\n"
                "• Applied marker: // MHEADER_APPLIED:name\n"
                "• Replaces everything above the marker; best near file tops\n"
                "• With Reference Tracking off, applied markers are cleaned for export\n\n"

                "ADVANCED RULES\n"
                "• Stored as advanced_snippet_injections/name.inject.json\n"
                "• Scope modes: file or directory using workspace-relative paths\n"
                "• New advanced rules default to file targets because they are safest\n"
                "• A rule can target multiple files/directories from the built-in editor\n"
                "• Directory targets apply to all eligible files inside that directory\n"
                "• Avoid targeting non-regenerated folders unless that is intentional\n"
                "• Match literal snippets or regex with flags such as IGNORECASE\n"
                "• Actions: replace_first or replace_all\n"
                "• Replacements can be inline or loaded from a snippet file\n"
                "• Create Advanced Injection Rule opens the form editor automatically\n"
                "• Double-click an advanced rule in its history tab to edit it later\n"
                "• History shows VALID, STALE, RULE CHANGED, or MISSING targets\n\n"

                "REPLACEMENTS.JSON\n"
                "• Exact find/replace patches for one workspace-relative file\n"
                "• Supports enabled, find, replace, and replace_mode: first\n"
                "• Useful for small targeted edits without a full override\n\n"

                "REFERENCE TRACKER\n"
                "• File Overrides: patch status and workspace counterpart\n"
                "• OUT OF SYNC file overrides queue one patch attempt automatically\n"
                "• Snippet/Import/Removal: snippet files, imports, removals, and targets\n"
                "• Headers: source files, unused entries, and targets\n"
                "• Advanced: rule history and stale/missing target checks\n"
                "• Selecting references syncs the Workspace and Patch trees\n\n"

                "CLEAN EXPORT MODE\n"
                "• Turn Reference Tracking off before a final regenerated export\n"
                "• Raw MSNIPPET/MHEADER/MIMPORT/MREMOVE markers still patch cleanly\n"
                "• No APPLIED tracking markers remain in clean export mode\n"
                "• Existing APPLIED markers are queued for cleanup when tracking is disabled\n"
                "• Once a file has no markers, snippet/header propagation intentionally stops\n\n"

                "FILE BACKUPS\n"
                "• Pre-edit backups are saved before patch writes and restores\n"
                "• Stored outside generator folders in file_backups/<version>-backups\n"
                "• Selecting a referenced file with backups opens a bottom panel\n"
                "• CURRENT shows the active workspace file beside stored backups\n"
                "• Open, reveal, or Switch To a backup without deleting current work\n\n"

                "COLOR INDICATORS\n"
                "• Green = healthy or applied\n"
                "• Red = missing\n"
                "• Yellow = modified, stale, or out of sync\n"
                "• Orange = disabled\n"
                "• Grey = unused\n"
                "• Cyan = mirrored/linked\n\n"

                "SUPPORT\n"
                "Discord: https://discord.gg/sQQPZQSEpS\n\n"

                "Developed by Vllax in Python (Codex-assisted)"
            ),
        )
        
        self.brand_help_text.configure(
            cursor="arrow",
        )
        
        self.brand_help_text.configure(
            state="disabled",
        )
        self.brand_help_scroll = ttk.Scrollbar(
            self.brand_help_frame,
            orient="vertical",
        )
        
        self.brand_help_scroll.pack(
            side=tk.RIGHT,
            fill=tk.Y,
        )
        
        
        self.brand_help_text.configure(
            yscrollcommand=self.brand_help_scroll.set,
        )
        
        self.brand_help_scroll.place(
            relx=1.0,
            x=-FOCUS_ZONE_WIDTH,
            y=0,
            anchor="ne",
            relheight=1.0,
        )
        # =====================================================
        # VISIBILITY STATE
        # =====================================================
        
        self.brand_help_visible = False
        
        self.brand_help_frame.place_forget()
        
        # =====================================================
        # POINTER HELPERS
        # =====================================================
        
        def pointer_inside_widget(widget):
        
            try:
        
                px, py = self.root.winfo_pointerxy()
        
            except Exception:
        
                return False
        
            x1 = widget.winfo_rootx()
            y1 = widget.winfo_rooty()
        
            x2 = x1 + widget.winfo_width()
            y2 = y1 + widget.winfo_height()
        
            return (
                x1 <= px <= x2
                and
                y1 <= py <= y2
            )
        
        
        # =====================================================
        # ACTIVITY
        # =====================================================

        activity_frame = ttk.LabelFrame(
            upper_right,
            text="Recent Workspace Activity",
        )
        
        activity_frame.pack(
            fill=tk.BOTH,
            expand=True,
            padx=5,
            pady=5,
        )
        
        activity_container = ttk.Frame(
            activity_frame
        )
        
        activity_container.pack(
            fill=tk.BOTH,
            expand=True,
        )
        
        activity_scrollbar = ttk.Scrollbar(
            activity_container,
            orient="vertical",
        )
        
        activity_scrollbar.pack(
            side=tk.RIGHT,
            fill=tk.Y,
        )
        
        self.activity_list = tk.Listbox(
            activity_container,
            height=10,
            yscrollcommand=activity_scrollbar.set,
        )
        
        self.activity_list.pack(
            side=tk.LEFT,
            fill=tk.BOTH,
            expand=True,
        )
        
        activity_scrollbar.config(
            command=self.activity_list.yview
        )
        
        

        # =====================================================
        # REFERENCE TRACKING
        # =====================================================
        
        self.reference_tracking_var = tk.BooleanVar(
            value=CONFIG.get(
                "reference_tracking",
                True,
            )
        )
        
        
        reference_outer = ttk.Frame(
            self.reference_vertical_pane
        )
        
        self.reference_vertical_pane.add(
            reference_outer,
            weight=3,
        )
        
        self.reference_vertical_pane.bind(
            "<ButtonRelease-1>",
            self.clamp_reference_pane,
        )
                
        def update_brand_help_visibility():
        
            inside_logo = pointer_inside_widget(
                self.brand_canvas
            )
        
            inside_focus_zone = (
                self.brand_help_visible
                and
                pointer_inside_brand_keepalive_zone()
            )
            keep_open = (
                inside_logo
                or inside_focus_zone
            )
        
            # =====================================
            # SHOW
            # =====================================
        
            if keep_open:
        
                if not self.brand_help_visible:
        
                    show_brand_help()
        
            # =====================================
            # HIDE
            # =====================================
        
            else:
        
                if self.brand_help_visible:
        
                    hide_brand_help()
        
            self.root.after(
                50,
                update_brand_help_visibility,
            )
        
        update_brand_help_visibility()

        
        def show_brand_help(_event=None):
        
            self.brand_help_visible = True
        
            update_brand_help_overlay()
            
        def update_brand_help_overlay():
        
            if not self.brand_help_visible:
                return
        
            self.root.update_idletasks()
        
            # =====================================
            # START BELOW STATUS PANEL
            # =====================================
        
            x = (
                upper_right.winfo_rootx()
                - self.root.winfo_rootx()
            )
        
            y = (
                status_frame.winfo_rooty()
                - self.root.winfo_rooty()
                + status_frame.winfo_height()
            )
            y -= 10
        
            # =====================================
            # COVER EVERYTHING BELOW
            # =====================================
        
            bottom = (
                self.right_vertical_pane.winfo_rooty()
                - self.root.winfo_rooty()
                + self.right_vertical_pane.winfo_height()
            )
        
            width = upper_right.winfo_width()
        
            height = max(
                100,
                bottom - y - 4,
            )
        
            self.brand_help_frame.place(
                x=x,
                y=y,
                width=width,
                height=height,
            )
        
            self.brand_help_frame.lift()
        
        def hide_brand_help():
        
            self.brand_help_visible = False
            
        
            self.brand_help_frame.place_forget()
        
            self.brand_help_frame.lower()
        
            self.root.update()
        
            self.right_vertical_pane.update()
        
            self.reference_vertical_pane.update()
        
            
        
        # =====================================
        # GLOBAL SCROLL DETECTION
        # =====================================
        
        self.brand_help_scroll_accumulator = 0
        
        # =====================================================
        # HEADER
        # =====================================================

        reference_header = ttk.Frame(
            reference_outer
        )

        reference_header.pack(
            fill=tk.X,
            padx=5,
            pady=(5, 0),
        )

        ttk.Checkbutton(
            reference_header,
            variable=self.reference_tracking_var,
            command=self.toggle_reference_tracking,
        ).pack(
            side=tk.LEFT,
        )

        ttk.Label(
            reference_header,
            text="Reference Tracking",
            font=header_font,
        ).pack(
            side=tk.LEFT,
            padx=(4, 10),
        )

        # =====================================================
        # NOTEBOOK
        # =====================================================

        self.reference_tabs = ttk.Notebook(
            reference_outer
        )
        
        self.reference_tabs.pack(
            fill=tk.BOTH,
            expand=True,
        )

        # =====================================================
        # FILE OVERRIDES TAB
        # =====================================================

        self.files_tab = ttk.Frame(
            self.reference_tabs
        )

        self.reference_tabs.add(
            self.files_tab,
            text="File Overrides",
        )

        self.files_tree = ttk.Treeview(
            self.files_tab,
            columns=("status",),
            show="tree headings",
            selectmode="browse"
        )

        self.files_tree.heading(
            "#0",
            text="File",
        )

        self.files_tree.heading(
            "status",
            text="Status",
        )

        self.files_tree.column(
            "#0",
            width=400,
        )

        self.files_tree.column(
            "status",
            width=120,
            anchor="center",
        )

        self.files_tree.pack(
            fill=tk.BOTH,
            expand=True,
        )

        # =====================================================
        # SNIPPET REFERENCES TAB
        # =====================================================

        self.snippet_tab = ttk.Frame(
            self.reference_tabs
        )

        self.reference_tabs.add(
            self.snippet_tab,
            text="Snippet / Import / Removal References",
        )

        self.snippet_tree = ttk.Treeview(
            self.snippet_tab,
            columns=(
                "type",
                "target",
                "source_path",
                "target_path",
            ),
            show="tree headings",
            selectmode="browse"
        )

        self.snippet_tree.heading(
            "#0",
            text="Reference",
        )

        self.snippet_tree.heading(
            "type",
            text="Type",
        )

        self.snippet_tree.heading(
            "target",
            text="Target",
        )

        self.snippet_tree.column(
            "#0",
            width=240,
        )

        self.snippet_tree.column(
            "type",
            width=120,
            anchor="center",
        )

        self.snippet_tree.column(
            "target",
            width=420,
        )

        self.snippet_tree.pack(
            fill=tk.BOTH,
            expand=True,
        )
        
        self.snippet_tree.column(
            "source_path",
            width=0,
            stretch=False,
        )
        
        self.snippet_tree.column(
            "target_path",
            width=0,
            stretch=False,
        )

        # =====================================================
        # HEADER REFERENCES TAB
        # =====================================================

        self.header_reference_tab = ttk.Frame(
            self.reference_tabs
        )

        self.reference_tabs.add(
            self.header_reference_tab,
            text="Header References",
        )

        self.header_reference_tree = ttk.Treeview(
            self.header_reference_tab,
            columns=(
                "type",
                "target",
                "source_path",
                "target_path",
            ),
            show="tree headings",
            selectmode="browse",
        )

        self.header_reference_tree.heading(
            "#0",
            text="Header",
        )

        self.header_reference_tree.heading(
            "type",
            text="Type",
        )

        self.header_reference_tree.heading(
            "target",
            text="Target",
        )

        self.header_reference_tree.column(
            "#0",
            width=240,
        )

        self.header_reference_tree.column(
            "type",
            width=120,
            anchor="center",
        )

        self.header_reference_tree.column(
            "target",
            width=420,
        )

        self.header_reference_tree.pack(
            fill=tk.BOTH,
            expand=True,
        )
        
        self.header_reference_tree.column(
            "source_path",
            width=0,
            stretch=False,
        )
        
        self.header_reference_tree.column(
            "target_path",
            width=0,
            stretch=False,
        )
        
        self.snippet_tree.bind(
            "<<TreeviewSelect>>",
            self.sync_reference_selection,
        )
        
        self.header_reference_tree.bind(
            "<<TreeviewSelect>>",
            self.sync_reference_selection,
        )
        
        self.files_tree.bind(
            "<<TreeviewSelect>>",
            self.sync_reference_selection,
        )
        
        

        # =====================================================
        # ADVANCED RULES TAB
        # =====================================================

        self.advanced_tab = ttk.Frame(
            self.reference_tabs
        )

        self.reference_tabs.add(
            self.advanced_tab,
            text="Advanced Rule Usage History",
        )

        self.advanced_tree = ttk.Treeview(
            self.advanced_tab,
            columns=(
                "status",
                "target",
                "last_applied",
                "path",
            ),
            show="tree headings",
            selectmode="browse"
        )

        self.advanced_tree.heading(
            "#0",
            text="Rule",
        )

        self.advanced_tree.heading(
            "status",
            text="Status",
        )

        self.advanced_tree.heading(
            "target",
            text="Target",
        )
        
        self.advanced_tree.heading(
            "last_applied",
            text="Last Applied",
        )

        self.advanced_tree.heading(
            "path",
            text="Path",
        )

        self.advanced_tree.column(
            "#0",
            width=260,
        )

        self.advanced_tree.column(
            "status",
            width=100,
            anchor="center",
        )

        self.advanced_tree.column(
            "target",
            width=420,
        )
        
        self.advanced_tree.column(
            "last_applied",
            width=180,
            anchor="center",
        )

        self.advanced_tree.column(
            "path",
            width=0,
            stretch=False,
        )

        self.advanced_tree.pack(
            fill=tk.BOTH,
            expand=True,
        )
        
        self.advanced_tree.column(
            "path",
            width=0,
            stretch=False,
        )

        self.advanced_tree.bind(
            "<Double-1>",
            self.open_selected_advanced_rule,
        )

        self.workspace_tree.bind(
            "<Double-1>",
            self.open_tree_item_on_double_click,
        )
        
        self.patch_tree.bind(
            "<Double-1>",
            self.open_tree_item_on_double_click,
        )
        
        self.files_tree.bind(
            "<Double-1>",
            self.open_tree_item_on_double_click,
        )
        
        self.advanced_tree.bind(
            "<<TreeviewSelect>>",
            self.sync_reference_selection,
        )

        # =====================================================
        # BACKUP SLIDE-UP PANEL
        # =====================================================

        self.backup_panel = tk.Frame(
            reference_outer,
            bg="#1f1f1f",
            bd=1,
            relief="solid",
        )

        backup_header = tk.Frame(
            self.backup_panel,
            bg="#1f1f1f",
        )

        backup_header.pack(
            fill=tk.X,
            padx=8,
            pady=(6, 2),
        )

        self.backup_panel_title = tk.Label(
            backup_header,
            text="File Backups",
            bg="#1f1f1f",
            fg="#7fdcff",
            font=("TkDefaultFont", 10, "bold"),
        )

        self.backup_panel_title.pack(
            side=tk.LEFT,
        )

        backup_buttons = ttk.Frame(
            backup_header,
        )

        backup_buttons.pack(
            side=tk.RIGHT,
        )

        self.backup_open_button = ttk.Button(
            backup_buttons,
            text="Open",
            command=self.open_selected_backup,
            takefocus=False,
        )

        self.backup_open_button.pack(
            side=tk.LEFT,
            padx=2,
        )

        self.backup_reveal_button = ttk.Button(
            backup_buttons,
            text="Reveal",
            command=self.reveal_selected_backup,
            takefocus=False,
        )

        self.backup_reveal_button.pack(
            side=tk.LEFT,
            padx=2,
        )

        self.backup_switch_button = ttk.Button(
            backup_buttons,
            text="Switch To",
            command=self.restore_selected_backup,
            takefocus=False,
        )

        self.backup_switch_button.pack(
            side=tk.LEFT,
            padx=2,
        )

        self.backup_close_button = ttk.Button(
            backup_buttons,
            text="Close",
            command=self.hide_backup_panel,
            takefocus=False,
        )

        self.backup_close_button.pack(
            side=tk.LEFT,
            padx=(8, 2),
        )

        for button, action in (
            (
                self.backup_open_button,
                self.open_selected_backup,
            ),
            (
                self.backup_reveal_button,
                self.reveal_selected_backup,
            ),
            (
                self.backup_switch_button,
                self.restore_selected_backup,
            ),
            (
                self.backup_close_button,
                self.hide_backup_panel,
            ),
        ):

            button.bind(
                "<ButtonPress-1>",
                lambda event, a=action:
                    self.invoke_backup_panel_action(
                        event,
                        a,
                    ),
            )

        backup_body = ttk.Frame(
            self.backup_panel,
        )

        backup_body.pack(
            fill=tk.BOTH,
            expand=True,
            padx=8,
            pady=(2, 8),
        )

        self.backup_tree = ttk.Treeview(
            backup_body,
            columns=(
                "reason",
                "hash",
                "size",
                "path",
            ),
            show="tree headings",
            selectmode="browse",
            height=4,
        )

        self.backup_tree.heading(
            "#0",
            text="Backup",
        )

        self.backup_tree.heading(
            "reason",
            text="Reason",
        )

        self.backup_tree.heading(
            "hash",
            text="Hash",
        )

        self.backup_tree.heading(
            "size",
            text="Size",
        )

        self.backup_tree.column(
            "#0",
            width=180,
        )

        self.backup_tree.column(
            "reason",
            width=120,
            anchor="center",
        )

        self.backup_tree.column(
            "hash",
            width=110,
            anchor="center",
        )

        self.backup_tree.column(
            "size",
            width=80,
            anchor="e",
        )

        self.backup_tree.column(
            "path",
            width=0,
            stretch=False,
        )

        backup_scroll = ttk.Scrollbar(
            backup_body,
            orient="vertical",
            command=self.backup_tree.yview,
        )

        self.backup_tree.configure(
            yscrollcommand=backup_scroll.set
        )

        backup_scroll.pack(
            side=tk.RIGHT,
            fill=tk.Y,
        )

        self.backup_tree.pack(
            side=tk.LEFT,
            fill=tk.BOTH,
            expand=True,
        )

        self.backup_tree.bind(
            "<Double-1>",
            lambda _event: self.open_selected_backup(),
        )

        self.reference_tabs.bind(
            "<<NotebookTabChanged>>",
            self.on_reference_tab_changed,
        )

        self.reference_tabs.bind(
            "<Configure>",
            self.position_backup_panel,
        )
        
        # =====================================================
        # LOGS
        # =====================================================
        
        log_frame = ttk.LabelFrame(
            self.reference_vertical_pane,
            text="Logs",
        )
        
        self.reference_vertical_pane.add(
            log_frame,
            weight=1,
        )
        
        self.log_widget = tk.Text(
            log_frame,
            wrap="word",
            state="disabled",
            height=8,
        )
        
        self.log_widget.pack(
            fill=tk.BOTH,
            expand=True,
        )
        
        self.log.attach_widget(
            self.log_widget
        )
        
        # =====================================================
        # SEARCH HOOKS
        # =====================================================
        
        self.workspace_search.trace_add(
            "write",
            self.on_workspace_search_changed,
        )
        
        self.patch_search.trace_add(
            "write",
            self.on_patch_search_changed,
        )
        # =====================================================
        # TAGS
        # =====================================================

        for tree in (
            self.workspace_tree,
            self.patch_tree,
            self.files_tree,
            self.advanced_tree,
            self.snippet_tree,
            self.header_reference_tree,
            self.backup_tree,
        ):
        
            tree.tag_configure(
                "ok",
                foreground="#45ff83",
            )
        
            tree.tag_configure(
                "missing",
                foreground="#ff5c5c",
            )
        
            tree.tag_configure(
                "disabled",
                foreground="#A65D5D",
            )
            
            tree.tag_configure(
                "unused",
                foreground="#808080",
            )
        
            tree.tag_configure(
                "modified",
                foreground="#ffe066",
            )
            tree.tag_configure(
                "placeholder",
                foreground="#888888",
            )

        self.root.after(
            0,
            lambda: self.apply_reference_tracking_tab_state(
                self.reference_tracking_var.get()
            ),
        )

    # =====================================================
    # WATCHER
    # =====================================================
    def refresh_toggle_button_state(self):
    
        selected = self.get_selected_path(
            self.patch_tree
        )
    
        if not selected:
    
            selected = self.get_selected_path(
                self.workspace_tree
            )
    
        self.update_toggle_button(selected)
    def update_toggle_button(self, path: Path | None):
    
        if not hasattr(self, "toggle_button"):
            return
    
        if not self.engine or not path:
    
            self.toggle_button.config(
                text="Disable"
            )
    
            return
    
        if self.engine.is_disabled(path):
    
            self.toggle_button.config(
                text="Enable"
            )
    
        else:
    
            self.toggle_button.config(
                text="Disable"
            )

    def queue_reference_marker_cleanup(self):

        if not self.engine:
            return

        queued = 0
        changed_paths = []

        for workspace_path in self.engine.iter_workspace_files():

            if not workspace_path.is_file():
                continue

            if (
                self.engine.get_real_suffix(workspace_path)
                not in ALLOWED_EXTENSIONS
            ):
                continue

            try:

                content = workspace_path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                )

            except Exception:
                continue

            if not self.engine.content_has_any_patch_marker(
                content,
                include_applied=True,
            ):
                continue

            override_path = (
                self.engine.to_patch_path(
                    workspace_path
                )
            )

            with self.lock:

                self.state.pending_files[
                    str(workspace_path)
                ] = PendingPatch(
                    changed_file=workspace_path,
                    override_path=override_path,
                    event_type="CLEAN_MARKERS",
                )

            changed_paths.append(workspace_path)
            queued += 1

        if not queued:
            return

        self.last_event_time = time.time()

        self.activity_queue.put(
            f"[CLEAN] queued {queued} marker files"
        )

        self.schedule_refresh(
            scope=REFRESH_PARTIAL,
            changed_paths=changed_paths,
        )

        self.log.log(
            f"Queued marker cleanup for {queued} files"
        )

    def apply_reference_tracking_tab_state(
        self,
        enabled,
    ):

        if not hasattr(self, "reference_tabs"):
            return

        try:

            self.reference_tabs.tab(
                self.files_tab,
                state="normal",
            )

            self.reference_tabs.tab(
                self.advanced_tab,
                state="normal",
            )

            self.reference_tabs.tab(
                self.snippet_tab,
                state=(
                    "normal"
                    if enabled
                    else "disabled"
                ),
            )

            self.reference_tabs.tab(
                self.header_reference_tab,
                state=(
                    "normal"
                    if enabled
                    else "disabled"
                ),
            )

            if not enabled:

                self.reference_tabs.select(
                    self.files_tab
                )

        except Exception:
            pass
     
    def toggle_reference_tracking(self):

        enabled = (
            self.reference_tracking_var.get()
        )

        CONFIG["reference_tracking"] = enabled

        save_config(CONFIG)

        if self.engine:

            self.engine.reference_tracking_enabled = enabled

            self.engine.injection_content_cache.clear()
            self.engine.injection_mtime_cache.clear()
            self.engine.header_content_cache.clear()
            self.engine.header_mtime_cache.clear()

            if not enabled:

                self.queue_reference_marker_cleanup()

        # =====================================================
        # NOTEBOOK BEHAVIOUR
        # =====================================================

        try:

            if enabled:

                # ---------------------------------------------
                # RE-ENABLE FULL NOTEBOOK
                # ---------------------------------------------

                self.reference_tabs.state(
                    ["!disabled"]
                )

            else:

                # ---------------------------------------------
                # SWITCH TO FILE OVERRIDES TAB
                # BEFORE DISABLING REFERENCE TABS
                # ---------------------------------------------

                try:

                    self.reference_tabs.select(
                        self.files_tab
                    )

                except Exception:
                    pass

                # ---------------------------------------------
                # DISABLE ONLY REFERENCE-DEPENDENT TABS
                # ---------------------------------------------

                self.reference_tabs.tab(
                    self.snippet_tab,
                    state="disabled",
                )

                self.reference_tabs.tab(
                    self.header_reference_tab,
                    state="disabled",
                )

            # -------------------------------------------------
            # ALWAYS KEEP THESE USABLE
            # -------------------------------------------------

            self.reference_tabs.tab(
                self.files_tab,
                state="normal",
            )

            self.reference_tabs.tab(
                self.advanced_tab,
                state="normal",
            )

            # -------------------------------------------------
            # RE-ENABLE REFERENCE TABS
            # -------------------------------------------------

            if enabled:

                self.reference_tabs.tab(
                    self.snippet_tab,
                    state="normal",
                )

                self.reference_tabs.tab(
                    self.header_reference_tab,
                    state="normal",
                )

        except Exception:
            pass

        # =====================================================
        # FORCE TREE REFRESH
        # =====================================================

        self.schedule_refresh(
            scope=REFRESH_PARTIAL
        )

        # =====================================================
        # LOGGING
        # =====================================================

        self.log.log(

            "Reference Tracking "

            + (
                "Enabled"
                if enabled
                else "Disabled"
            )
        )

        # =====================================================
        # WARNING
        # =====================================================

        if not enabled:

            messagebox.showwarning(

                "Reference Tracking Disabled",

                (
                    "Reference Tracking has been disabled.\n\n"

                    "MSNIPPET, MHEADER, MIMPORT, and MREMOVE\n"
                    "markers will no longer be converted into\n"
                    "persistent tracking markers.\n\n"

                    "Existing APPLIED markers have been queued\n"
                    "for cleanup. Regenerated raw markers will\n"
                    "still patch, but no tracking markers will\n"
                    "be left behind.\n\n"

                    "MIMPORT lines become normal imports, and\n"
                    "MREMOVE blocks are removed cleanly.\n\n"

                    "Live relationship propagation between trees\n"
                    "and references will stop once files are marker-free."
                ),
            )
    def setup_watcher(self):
    
        if not self.engine:
            return
    
        if (
            self.observer and
            self.observer.is_alive()
        ):
    
            try:
    
                self.observer.stop()
    
                self.observer.join(timeout=2)
    
            except Exception:
                pass
    
        self.observer = Observer()
    
        workspace_handler = WorkspaceWatcher(self)
    
        patch_handler = PatchWatcher(self)
    
        workspace_root = str(
            self.engine.workspace_root.resolve()
        )
    
        patch_root = str(
            self.engine.patch_root.resolve()
        )
    
        self.observer.schedule(
            workspace_handler,
            workspace_root,
            recursive=True,
        )
    
        self.observer.schedule(
            patch_handler,
            patch_root,
            recursive=True,
        )
    
        self.observer.start()

    # =====================================================
    # BACKUP PANEL
    # =====================================================

    def format_backup_timestamp(
        self,
        timestamp,
    ):

        try:

            return time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(timestamp),
            )

        except Exception:

            return "Unknown"


    def format_backup_size(
        self,
        size,
    ):

        try:

            size = int(size)

        except Exception:

            return ""

        if size >= 1024 * 1024: 

            return f"{size / (1024 * 1024):.1f} MB"

        if size >= 1024:

            return f"{size / 1024:.1f} KB"

        return f"{size} B"


    def get_active_reference_tree(self):

        if not hasattr(self, "reference_tabs"):
            return None

        try:

            selected = self.reference_tabs.select()

            if selected == str(self.files_tab):
                return self.files_tree

            if selected == str(self.snippet_tab):
                return self.snippet_tree

            if selected == str(self.header_reference_tab):
                return self.header_reference_tree

            if selected == str(self.advanced_tab):
                return self.advanced_tree

        except Exception:
            return None

        return None


    def resolve_backup_target_from_reference_tree(
        self,
        tree,
    ):

        if not self.engine or not tree:
            return None

        selection = tree.selection()

        if not selection:
            return None

        values = tree.item(
            selection[0],
            "values",
        )

        if not values:
            return None

        candidate = None

        try:

            if tree == self.files_tree:

                if len(values) >= 2 and values[1]:

                    candidate = (
                        self.engine
                        .get_workspace_counterpart(
                            Path(values[1])
                        )
                    )

            elif tree in (
                self.snippet_tree,
                self.header_reference_tree,
            ):

                if len(values) >= 4 and values[3]:

                    candidate = Path(values[3])

            elif tree == self.advanced_tree:

                if len(values) >= 4 and values[3]:

                    candidate = Path(values[3])

        except Exception:

            candidate = None

        if not candidate:
            return None

        if not self.engine.get_backup_relative_path(
            candidate
        ):
            return None

        return candidate


    def show_backup_panel_for_target(
        self,
        target: Path,
    ):

        if (
            not self.engine
            or not hasattr(self, "backup_panel")
        ):
            return

        backups = self.engine.get_backups_for_file(
            target
        )

        if not backups:

            self.hide_backup_panel()

            return

        self.current_backup_target = target
        self.backup_entry_map = {}

        self.backup_panel_title.config(
            text=(
                f"Backups for {target.name} "
                f"({len(backups)} stored)"
            )
        )

        self.backup_tree.delete(
            *self.backup_tree.get_children()
        )

        first_item = None

        current_hash = ""
        current_size = ""

        if target.exists() and target.is_file():

            current_hash = (
                self.engine.compute_file_hash(target)
                or ""
            )

            try:

                current_size = self.format_backup_size(
                    target.stat().st_size
                )

            except Exception:

                current_size = ""

        current_size_bytes = 0

        try:

            if target.exists() and target.is_file():

                current_size_bytes = target.stat().st_size

        except Exception:

            current_size_bytes = 0

        current_entry = {

            "id":
                CURRENT_BACKUP_ROW_ID,

            "is_current":
                True,

            "target":
                str(target),

            "backup_file":
                str(target),

            "content_hash":
                current_hash,

            "size":
                current_size_bytes,
        }

        self.backup_entry_map[
            CURRENT_BACKUP_ROW_ID
        ] = current_entry

        first_item = self.backup_tree.insert(
            "",
            "end",
            iid=CURRENT_BACKUP_ROW_ID,
            text="CURRENT",
            values=(
                "active file",
                current_hash[:12],
                current_size,
                str(target),
            ),
            tags=("ok",),
        )

        for index, entry in enumerate(backups):

            backup_id = entry.get("id")

            if not backup_id:
                continue

            item_id = f"backup_{index}_{backup_id}"

            self.backup_entry_map[
                item_id
            ] = entry

            item = self.backup_tree.insert(
                "",
                "end",
                iid=item_id,
                text=self.format_backup_timestamp(
                    entry.get("timestamp", 0)
                ),
                values=(
                    entry.get("reason", ""),
                    entry.get("content_hash", "")[:12],
                    self.format_backup_size(
                        entry.get("size", 0)
                    ),
                    entry.get("backup_file", ""),
                ),
            )

        if first_item:

            self.backup_tree.selection_set(first_item)
            self.backup_tree.focus(first_item)

        self.backup_panel_visible = True

        self.position_backup_panel()


    def update_backup_panel_for_reference_selection(
        self,
        tree=None,
    ):

        if tree is None:
            tree = self.get_active_reference_tree()

        target = self.resolve_backup_target_from_reference_tree(
            tree
        )

        if not target:

            self.hide_backup_panel()

            return

        self.show_backup_panel_for_target(target)


    def position_backup_panel(
        self,
        _event=None,
    ):

        if (
            not self.backup_panel_visible
            or not hasattr(self, "backup_panel")
        ):
            return

        try:

            self.reference_tabs.update_idletasks()

            width = self.reference_tabs.winfo_width()
            height = self.reference_tabs.winfo_height()

            if width <= 0 or height <= 0:
                return

            panel_margin = 28
            panel_bottom_margin = 26

            panel_height = min(
                165,
                max(
                    118,
                    height // 3,
                ),
            )

            self.backup_panel.place(
                x=(
                    self.reference_tabs.winfo_x()
                    + panel_margin
                ),
                y=(
                    self.reference_tabs.winfo_y()
                    + height
                    - panel_height
                    - panel_bottom_margin
                ),
                width=max(
                    240,
                    width - (panel_margin * 2),
                ),
                height=panel_height,
            )

            self.backup_panel.lift()

        except Exception:
            pass


    def hide_backup_panel(self):

        if not hasattr(self, "backup_panel"):
            return

        self.backup_panel_visible = False
        self.current_backup_target = None
        self.backup_entry_map = {}

        try:

            self.backup_panel.place_forget()

        except Exception:
            pass


    def on_reference_tab_changed(
        self,
        _event=None,
    ):

        if self.selection_side_effects_suppressed():
            return

        if self.is_refreshing_trees:
            return

        self.update_backup_panel_for_reference_selection()


    def invoke_backup_panel_action(
        self,
        event,
        action,
    ):

        try:

            if hasattr(self, "backup_panel"):

                self.backup_panel.lift()
                self.backup_panel.focus_set()

            action()

        except Exception as e:

            self.log.log(
                f"Backup panel action failed: {e}"
            )

        return "break"


    def get_selected_backup_entry(self):

        if not hasattr(self, "backup_tree"):
            return None

        selection = self.backup_tree.selection()

        if not selection:
            return None

        return self.backup_entry_map.get(
            selection[0]
        )


    def get_selected_backup_path(self):

        entry = self.get_selected_backup_entry()

        if not entry:
            return None

        if entry.get("is_current"):

            path = Path(entry.get("target", ""))

        else:

            path = Path(entry.get("backup_file", ""))

        return path


    def open_selected_backup(self):

        path = self.get_selected_backup_path()

        if not path:
            return

        self.open_path(path)


    def reveal_path(
        self,
        path: Path,
    ):

        if not path or not path.exists():
            return

        try:

            if sys.platform == "darwin":

                subprocess.Popen([
                    "open",
                    "-R",
                    str(path),
                ])

            elif os.name == "nt":

                subprocess.Popen([
                    "explorer",
                    f"/select,{path}",
                ])

            else:

                subprocess.Popen([
                    "xdg-open",
                    str(path.parent),
                ])

        except Exception as e:

            messagebox.showerror(
                "Reveal Failed",
                str(e),
            )


    def reveal_selected_backup(self):

        path = self.get_selected_backup_path()

        if not path:
            return

        self.reveal_path(path)


    def restore_selected_backup(self):

        if not self.engine:
            return

        entry = self.get_selected_backup_entry()

        if not entry:
            return

        if entry.get("is_current"):

            self.log.log(
                "Backup switch skipped: current file is already active."
            )

            return

        target = self.engine.restore_backup(entry)

        if not target:

            messagebox.showerror(
                "Restore Failed",
                "That backup could not be restored.",
            )

            return

        self.schedule_refresh(
            scope=REFRESH_FULL,
            changed_paths=[target],
        )

        self.show_backup_panel_for_target(target)

        self.select_path_in_tree(
            self.workspace_tree,
            target,
            additive=False,
        )

    # =====================================================
    # FILE ACTIONS
    # =====================================================
   
    def get_selected_path(self, tree):
    
        item = tree.focus()
    
        if not item:
            return None
    
        values = tree.item(item, "values")
    
        if not values:
            return None
    
        possible_paths = []
    
        if tree in (
            self.snippet_tree,
            self.header_reference_tree,
        ):
    
            if len(values) >= 3 and values[2]:
                possible_paths.append(values[2])
    
            if len(values) >= 4 and values[3]:
                possible_paths.append(values[3])
    
        elif tree == self.files_tree:
    
            if len(values) >= 2 and values[1]:
                possible_paths.append(values[1])
    
        elif tree == self.advanced_tree:
    
            if len(values) >= 4 and values[3]:
                possible_paths.append(values[3])
    
        else:
    
            if values[0]:
                possible_paths.append(values[0])
    
        for raw_path in possible_paths:
    
            if raw_path in (
                "",
                "__separator__",
                "__placeholder__",
            ):
                continue
    
            try:
                return Path(raw_path)
    
            except Exception:
                pass
    
        return None
    def toggle_patching_enabled(self):
    
        enabled = (
            not self.patching_enabled.get()
        )
        self.log.log(
            "Patch system ENABLED"
            if enabled
            else "Patch system DISABLED"
        )
        self.patching_enabled.set(enabled)
    
        self.update_branding_state()

        if enabled and self.engine:

            self.schedule_refresh(
                scope=REFRESH_FULL
            )
    
    
    def update_branding_state(self):
    
        enabled = self.patching_enabled.get()
        state_font_size = 14
        if enabled:
    
            title_color = "#45ff83"
            subtitle_color = "#7fdcff"
    
            state_text = "PATCHING ENABLED"
            state_color = "#45ff83"
    
            bg = "#242424"
            outline = "#2f2f2f"
    
        else:
    
            title_color = "#ff5c5c"
            subtitle_color = "#ff9a9a"
    
            state_text = "PATCHING DISABLED"
            state_color = "#ff5c5c"
    
            bg = "#2b1f1f"
            outline = "#5a2a2a"
    
        self.brand_title.configure(
            fg=title_color,
            bg=bg,
        )
        
        self.brand_title.configure(
            cursor="fleur"
        )
        
        self.brand_subtitle.configure(
            cursor="fleur"
        )
        
        self.brand_state_label.configure(
            cursor="fleur"
        )
    
        self.brand_subtitle.configure(
            fg=subtitle_color,
            bg=bg,
        )
    
        self.brand_state_label.configure(
            text=state_text,
            fg=state_color,
            bg=bg,
            font=(
                "TkDefaultFont",
                state_font_size,
                "bold",
            ),
        )
        
        self.brand_canvas.configure(
            bg=bg,
        )
    
        self.brand_canvas.itemconfig(
            "background",
            fill=bg,
            outline=outline,
        )
    def get_selected_paths(self, tree):
    
        paths = []
        seen = set()
    
        selection = set(
            tree.selection()
        )
    
        for item in tree.selection():
    
            # =====================================
            # SKIP CHILD IF PARENT ALSO SELECTED
            # =====================================
    
            parent = tree.parent(item)
    
            skip = False
    
            while parent:
    
                if parent in selection:
    
                    skip = True
                    break
    
                parent = tree.parent(parent)
    
            if skip:
                continue
    
            # =====================================
            # IGNORE SEPARATORS
            # =====================================
    
            if self.tree_manager.is_separator_item(
                tree,
                item,
            ):
                continue
    
            values = tree.item(
                item,
                "values",
            )
    
            if not values:
                continue
    
            possible_paths = []
    
            # =====================================
            # REFERENCE TREES
            # =====================================
    
            if tree in (
                self.snippet_tree,
                self.header_reference_tree,
            ):
    
                if len(values) >= 3 and values[2]:
                    possible_paths.append(values[2])
    
                if len(values) >= 4 and values[3]:
                    possible_paths.append(values[3])
    
            # =====================================
            # FILE OVERRIDES TREE
            # =====================================
    
            elif tree == self.files_tree:
    
                if len(values) >= 2 and values[1]:
                    possible_paths.append(values[1])
    
            # =====================================
            # ADVANCED TREE
            # =====================================
    
            elif tree == self.advanced_tree:
    
                if len(values) >= 4 and values[3]:
                    possible_paths.append(values[3])
    
            # =====================================
            # NORMAL TREES
            # =====================================
    
            else:
    
                if values[0]:
                    possible_paths.append(values[0])
    
            # =====================================
            # NORMALIZE
            # =====================================
    
            for raw_path in possible_paths:
    
                if raw_path in (
                    "",
                    "__separator__",
                    "__placeholder__",
                ):
                    continue
    
                try:
    
                    path = Path(raw_path)
    
                except Exception:
                    continue
    
                normalized = str(path)
    
                if normalized in seen:
                    continue
    
                seen.add(normalized)
    
                paths.append(path)
    
        return paths
    def open_selected_file(self, tree):
    
        paths = self.get_selected_paths(tree)
    
        if not paths:
            return
    
        for path in paths:
    
            if not path.is_file():
                continue
    
            self.open_path(path)
    
    def open_tree_item_on_double_click(self, event):
    
        tree = event.widget
    
        item = tree.identify_row(event.y)
    
        if not item:
            return
    
        values = tree.item(
            item,
            "values",
        )
    
        if not values:
            return
    
        raw_path = values[-1]
    
        if raw_path in (
            "__separator__",
            "__placeholder__",
            "",
        ):
            return
    
        path = Path(raw_path)
    
        if not path.exists():
            return
        
        # Ignore folders on double click
        if not path.is_file():
            return
        
        self.open_path(path)
    
    def expand_to_node(
        self,
        tree,
        node,
    ):
        parent = tree.parent(node)
    
        while parent:
    
            tree.item(parent, open=True)
    
            parent = tree.parent(parent)
            
    def reveal_selected_file(self, tree):
    
        paths = self.get_selected_paths(tree)
    
        if not paths:
            return
    
        for path in paths:

            self.reveal_path(path)
    
    
    def reselect_recursive(
        self,
        tree,
        node,
        target_path,
        additive=True,
    ):
    
        values = tree.item(node, "values")
        
        if not values:
            return False
        
        if values[0] == "__separator__":
            return False
        
        # =====================================
        # REFERENCE TREES
        # =====================================
        
        if tree in (
            self.snippet_tree,
            self.header_reference_tree,
        ):
        
            possible_paths = []
        
            if len(values) >= 3 and values[2]:
                possible_paths.append(values[2])
        
            if len(values) >= 4 and values[3]:
                possible_paths.append(values[3])
        
        # =====================================
        # FILE OVERRIDES TREE
        # =====================================
        
        elif tree == self.files_tree:
        
            possible_paths = []
        
            if len(values) >= 2 and values[1]:
                possible_paths.append(values[1])
        
        # =====================================
        # NORMAL TREES
        # =====================================
        
        else:
        
            possible_paths = [values[0]]
        
        for raw_path in possible_paths:
        
            try:
        
                current = Path(raw_path)
        
            except Exception:
                continue
        
            current_relative = (
                self.engine.get_relative_path(
                    self.engine.normalized_disabled_path(
                        current
                    )
                )
            )
        
            target_relative = (
                self.engine.get_relative_path(
                    self.engine.normalized_disabled_path(
                        target_path
                    )
                )
            )
        
            if current_relative == target_relative:
        
                self.expand_to_node(
                    tree,
                    node,
                )
                
                # =====================================
                # REFERENCE TREES NEVER MULTI-SELECT
                # =====================================
                
                if tree in (
                    self.snippet_tree,
                    self.header_reference_tree,
                    self.files_tree,
                    self.advanced_tree,
                ):
                
                    tree.selection_set(node)
                
                else:
                
                    if node not in tree.selection():
                
                        if additive:
                
                            tree.selection_add(node)
                
                        else:
                
                            tree.selection_set(node)
                tree.focus(node)
                tree.see(node)
        
                return True
    
        for child in tree.get_children(node):
    
            if self.reselect_recursive(
                tree,
                child,
                target_path,
                additive=additive,
            ):
    
                return True
    
        return False
    def toggle_selected_file(self, tree):
    
        if not self.require_workspace():
            return
    
        paths = self.get_selected_paths(tree)
    
        if not paths:
            return
    
        try:
    
            reenabled_paths = []
            affected_workspace_files = set()
    
            # =====================================
            # TOGGLE FILES
            # =====================================
    
            for path in paths:
    
                if path.name in SYSTEM_FILES:
                    continue
    
                if not path.is_file():
                    continue
    
                if path.name == PLACEHOLDER_FILENAME:
                    continue
    
                # =====================================
                # DISABLE
                # =====================================
    
                if not self.engine.is_disabled(path):
    
                    new_path = Path(
                        str(path) + DISABLED_EXTENSION
                    )
    
                    path.rename(new_path)
    
                    self.log.log(
                        f"Disabled: {path.name}"
                    )
    
                # =====================================
                # ENABLE
                # =====================================
    
                else:
    
                    new_path = Path(
                        str(path).replace(
                            DISABLED_EXTENSION,
                            "",
                        )
                    )
    
                    path.rename(new_path)
    
                    reenabled_paths.append(
                        new_path
                    )
    
                    self.log.log(
                        f"Enabled: {new_path.name}"
                    )
    
            # =====================================
            # REFRESH ROOTS ONCE
            # =====================================
    
            self.engine.injection_root = (
                self.engine.resolve_optional_disabled_folder(
                    self.engine.patch_root
                    / INJECTION_FOLDER_NAME
                )
            )
    
            self.engine.header_root = (
                self.engine.resolve_optional_disabled_folder(
                    self.engine.patch_root
                    / HEADER_FOLDER_NAME
                )
            )
    
            self.engine.advanced_injection_root = (
                self.engine.resolve_optional_disabled_folder(
                    self.engine.patch_root
                    / ADVANCED_INJECTION_FOLDER_NAME
                )
            )
    
            self.engine.rebuild_advanced_rule_cache()
    
            # =====================================
            # COLLECT AFFECTED FILES
            # =====================================
    
            for path in reenabled_paths:
    
                try:
    
                    if path.is_relative_to(
                        self.engine.injection_root
                    ):
    
                        for workspace_path in (
                            self.engine.iter_workspace_files()
                        ):
    
                            try:
    
                                preview = workspace_path.read_text(
                                    encoding="utf-8",
                                    errors="ignore",
                                )
    
                            except Exception:
                                continue
    
                            if not RAW_INJECTION_PATTERN.search(
                                preview
                            ):
                                continue
    
                            affected_workspace_files.add(
                                workspace_path
                            )
    
                except Exception:
                    pass
    
                try:
    
                    if path.is_relative_to(
                        self.engine.header_root
                    ):
    
                        for workspace_path in (
                            self.engine.iter_workspace_files()
                        ):
    
                            try:
    
                                preview = workspace_path.read_text(
                                    encoding="utf-8",
                                    errors="ignore",
                                )
    
                            except Exception:
                                continue
    
                            if not RAW_HEADER_PATTERN.search(
                                preview
                            ):
                                continue
    
                            affected_workspace_files.add(
                                workspace_path
                            )
    
                except Exception:
                    pass
    
                workspace_file = (
                    self.engine.get_workspace_counterpart(
                        path
                    )
                )
    
                if workspace_file.exists():
    
                    affected_workspace_files.add(
                        workspace_file
                    )
    
                    # =====================================
                    # IMMEDIATE PROPAGATION
                    # ONLY WHEN PATCHING ENABLED
                    # =====================================
    
                    if self.patching_enabled.get():
    
                        try:
    
                            self.engine.patch_file(
                                workspace_file,
                                path,
                            )
    
                        except Exception as e:
    
                            self.log.log(
                                f"Immediate patch failed: {e}"
                            )
    
            # =====================================
            # APPLY ARBITRARY FILES
            # ONLY WHEN PATCHING ENABLED
            # =====================================
    
            if self.patching_enabled.get():
    
                self.engine.apply_arbitrary_files()
    
            # =====================================
            # REFRESH ONCE
            # =====================================
    
            self.schedule_refresh(
                scope=REFRESH_FULL
            )
    
            self.refresh_toggle_button_state()
    
            # =====================================
            # RESELECT
            # =====================================
    
            if paths:
    
                final_path = (
                    reenabled_paths[-1]
                    if reenabled_paths
                    else paths[-1]
                )
    
                self.root.after(
                    50,
                    lambda: self.select_path_in_tree(
                        self.patch_tree,
                        final_path,
                    ),
                )
    
        except Exception as e:
    
            messagebox.showerror(
                "Toggle Failed",
                str(e),
            )
      
    def update_status(self, selected_path: Path):
    
        # =====================================================
        # NO WORKSPACE
        # =====================================================
    
        if not self.engine:
    
            status_text = (
                "No workspace loaded.\n\n"
                "Use the Workspace menu to add or select a workspace."
            )
    
            self.status_text.configure(
                state="normal"
            )
    
            self.status_text.delete(
                "1.0",
                tk.END,
            )
    
            self.status_text.insert(
                "1.0",
                status_text,
            )
    
            self.status_text.configure(
                state="disabled"
            )
    
            return
    
        lines = [
            f"Selected:\n{selected_path}",
            "",
        ]
    
        # =====================================================
        # FILE INFO
        # =====================================================
    
        if selected_path.is_file():
    
            try:
    
                lines.extend([
                    f"Size: {selected_path.stat().st_size} bytes",
                    f"Extension: {selected_path.suffix}",
                ])
    
            except Exception:
                pass
    
        else:
    
            lines.append("Directory")
    
        # =====================================================
        # HEADER INJECTION
        # =====================================================
    
        if (
            selected_path.parent
            == self.engine.header_root
        ):
    
            header_name = self.engine.normalize_marker_name(
                selected_path.stem
                .replace(DISABLED_EXTENSION, "")
            )
    
            lines.extend([
                "",
                "Header Injection",
                "",
                "Use this in MCreator custom code:",
                "",
                f"// MHEADER:{header_name}",
                "",
                "Everything ABOVE this marker",
                "will be replaced with the",
                "contents of this file, when the generator matches.",
            ])
    
        # =====================================================
        # SNIPPET INJECTION
        # =====================================================
    
        if (
            selected_path.parent
            == self.engine.injection_root
        ):
    
            injection_name = self.engine.normalize_marker_name(
                selected_path.stem
                .replace(DISABLED_EXTENSION, "")
            )
    
            lines.extend([
                "",
                "Snippet Injection",
                "",
                "Use this in MCreator custom code:",
                "",
                f"// MSNIPPET:{injection_name}",
                "",
                "When patched, this marker",
                "will be replaced with the",
                "contents of this file, when the generator matches.",
            ])
    
        # =====================================================
        # PATCH STATUS
        # =====================================================
        
        globally_disabled = (
            not self.patching_enabled.get()
        )
        
        if self.engine.is_disabled(selected_path):
        
            lines.extend([
                "",
                "Status: DISABLED",
                "",
                "• This patch file will NOT",
                "  synchronise to the workspace.",
            ])
        
            if globally_disabled:
        
                lines.extend([
                    "",
                    "• GLOBAL PATCHING IS DISABLED.",
                    "• No automatic patching is active.",
                ])
        
            else:
        
                lines.extend([
                    "",
                    "• Automatic patch application",
                    "  is currently disabled.",
                ])
        
        else:
        
            counterpart = None
        
            # =============================================
            # SELECTED FROM WORKSPACE
            # =============================================
        
            try:
        
                if selected_path.is_relative_to(
                    self.engine.workspace_root
                ):
        
                    counterpart = (
                        self.engine.to_patch_path(
                            selected_path
                        )
                    )
        
                # =========================================
                # SELECTED FROM PATCH TREE
                # =========================================
        
                elif selected_path.is_relative_to(
                    self.engine.patch_root
                ):
        
                    counterpart = (
                        self.engine.to_workspace_path(
                            selected_path
                        )
                    )
        
            except Exception:
                pass
        
            if counterpart and counterpart.exists():
        
                lines.extend([
                    "",
                    "Status: ACTIVE PATCH",
                    "",
                    "• This file participates",
                    "  in automatic synchronisation.",
                ])
        
                if globally_disabled:
        
                    lines.extend([
                        "",
                        "• GLOBAL PATCHING IS CURRENTLY DISABLED.",
                        "• Workspace monitoring remains active.",
                        "• No patches are currently being applied.",
                    ])
    
        # =====================================================
        # FINAL OUTPUT
        # =====================================================
    
        status_text = "\n".join(lines)
    
        self.status_text.configure(
            state="normal"
        )
    
        self.status_text.delete(
            "1.0",
            tk.END,
        )
    
        self.status_text.insert(
            "1.0",
            status_text,
        )
    
        self.status_text.see(
            "1.0"
        )
    
        self.status_text.configure(
            state="disabled"
        )
    
    def apply_tree_model_chunked(
        self,
        tree,
        model,
        node_map,
        chunk_size=100,
    ):
    
        expanded = (
            self.tree_manager.capture_expanded(
                tree
            )
        )
    
        selected = (
            self.tree_manager.capture_selected(
                tree
            )
        )
    
        tree.delete(
            *tree.get_children()
        )
    
        node_map.clear()
    
        queue = [
            (
                "",
                model,
            )
        ]
    
        def process_chunk():
        
            processed = 0
        
            while queue and processed < chunk_size:
        
                parent_id, node = queue.pop(0)
        
                node_type = node.get(
                    "type",
                    "normal",
                )
        
                # =====================================
                # SEPARATOR NODE
                # =====================================
        
                if node_type == "separator":
        
                    item = tree.insert(
        
                        parent_id,
        
                        "end",
        
                        text=node["display"],
        
                        open=False,
        
                        values=["__separator__"],
        
                        tags=node["tags"],
                    )
        
                # =====================================
                # NORMAL NODE
                # =====================================
        
                else:
        
                    path = node["path"]
        
                    relative = (
                        self.engine.get_relative_path(
                            path
                        )
                    )
        
                    item = tree.insert(
        
                        parent_id,
        
                        "end",
        
                        text=node["display"],
        
                        open=relative in expanded,
        
                        values=[str(path)],
        
                        tags=node["tags"],
                    )
        
                    if relative:
        
                        node_map[relative] = item
        
                # =====================================
                # QUEUE CHILDREN
                # =====================================
        
                for child in reversed(
                    node.get(
                        "children",
                        [],
                    )
                ):
        
                    queue.insert(
                        0,
                        (
                            item,
                            child,
                        )
                    )
        
                processed += 1
        
            # =========================================
            # CONTINUE NEXT CHUNK
            # =========================================
        
            if queue:
        
                self.root.after(
                    1,
                    process_chunk,
                )
        
            # =========================================
            # FINISHED
            # =========================================
        
            else:
        
                self.tree_manager.restore_selected(
                    tree,
                    node_map,
                    selected,
                )
        
        process_chunk()
    
    
    def apply_reference_model(
        self,
        model,
    ):
    
        self.apply_reference_files_model(
            model["files"]
        )
    
        self.apply_reference_snippets_model(
            model["snippets"]
        )
    
        self.apply_reference_headers_model(
            model["headers"]
        )
    
        self.apply_reference_advanced_model(
            model["advanced"]
        )

        if self.backup_panel_visible:

            self.update_backup_panel_for_reference_selection()

        self.queue_out_of_sync_file_patches(
            model["files"]
        )


    def queue_out_of_sync_file_patches(
        self,
        files_model,
    ):

        if not self.engine:
            return

        if not self.patching_enabled.get():
            return

        queued = 0
        copied = 0
        active_paths = set()
        copy_jobs = []

        with self.lock:

            for entry in files_model:

                if entry.get("disabled"):
                    continue

                target_exists = bool(
                    entry.get("exists")
                )

                if (
                    target_exists
                    and not entry.get("modified")
                ):
                    continue

                patch_path = entry.get("path")

                if not patch_path:
                    continue

                try:

                    patch_path = Path(patch_path)

                    if not patch_path.is_file():
                        continue

                    workspace_path = (
                        self.engine.get_workspace_counterpart(
                            patch_path
                        )
                    )

                    patch_hash = (
                        self.engine.compute_file_hash(
                            patch_path
                        )
                    )

                    if patch_hash is None:
                        continue

                    workspace_exists = (
                        workspace_path.exists()
                    )

                    workspace_hash = "MISSING"

                    if workspace_exists:

                        workspace_hash = (
                            self.engine.compute_file_hash(
                                workspace_path
                            )
                        )

                        if workspace_hash is None:
                            continue

                    attempt_key = (
                        f"{patch_hash}:{workspace_hash}"
                    )

                    state_key = str(patch_path)

                    active_paths.add(state_key)

                    if (
                        self.state.out_of_sync_attempts.get(
                            state_key
                        )
                        == attempt_key
                    ):
                        continue

                    if (
                        self.engine.get_real_suffix(
                            patch_path
                        )
                        not in ALLOWED_EXTENSIONS
                    ):

                        self.state.out_of_sync_attempts[
                            state_key
                        ] = attempt_key

                        copy_jobs.append(
                            (
                                patch_path,
                                workspace_path,
                            )
                        )

                        continue

                    if (
                        str(workspace_path)
                        in self.state.pending_files
                    ):
                        continue

                    self.state.out_of_sync_attempts[
                        state_key
                    ] = attempt_key

                    self.state.pending_files[
                        str(workspace_path)
                    ] = PendingPatch(
                        changed_file=workspace_path,
                        override_path=patch_path,
                        event_type=(
                            "OUT_OF_SYNC"
                            if workspace_exists
                            else "MISSING_OVERRIDE"
                        ),
                    )

                    queued += 1

                except Exception as e:

                    self.log.log(
                        f"Out-of-sync queue failed: {e}"
                    )

            stale_keys = [
                key for key in self.state
                .out_of_sync_attempts.keys()
                if key not in active_paths
            ]

            for key in stale_keys:

                self.state.out_of_sync_attempts.pop(
                    key,
                    None,
                )

        changed_paths = []

        for patch_path, workspace_path in copy_jobs:

            try:

                self.engine.backup_workspace_file(
                    workspace_path,
                    reason="pre_out_of_sync_copy",
                )

                workspace_path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                shutil.copy2(
                    patch_path,
                    workspace_path,
                )

                self.state.recently_patched[
                    str(workspace_path)
                ] = time.time()

                changed_paths.extend([
                    patch_path,
                    workspace_path,
                ])

                copied += 1

            except Exception as e:

                self.log.log(
                    f"Out-of-sync copy failed: {e}"
                )

        if copied:

            self.schedule_refresh(
                scope=REFRESH_FULL,
                changed_paths=changed_paths,
            )

        if not queued and not copied:
            return

        self.last_event_time = time.time()

        self.activity_queue.put(
            (
                f"[OUT OF SYNC] queued {queued} files, "
                f"copied {copied}"
            )
        )

        self.log.log(
            (
                "Out-of-sync file overrides handled: "
                f"{queued} queued, {copied} copied"
            )
        )


    def apply_reference_files_model(
        self,
        files_model,
    ):
    
        self.files_tree.delete(
            *self.files_tree.get_children()
        )
    
        file_root_nodes = {}
    
        for entry in files_model:
    
            path = entry["path"]
    
            exists = entry["exists"]
            modified = entry["modified"]
            disabled = entry["disabled"]
    
            relative = (
                self.engine.get_relative_path(path)
                or path.name
            )
    
            parent_folder = str(
                Path(relative).parent
            )
    
            if parent_folder in ("", "."):
                parent_folder = "ROOT"
    
            if parent_folder not in file_root_nodes:
    
                file_root_nodes[parent_folder] = (
                    self.files_tree.insert(
                        "",
                        "end",
                        text=f"📁 {parent_folder}",
                        open=True,
                        tags=("folder",),
                    )
                )
    
            status = []
            tags = []
    
            if disabled:
    
                status.append("DISABLED")
                tags.append("disabled")
    
            elif not exists:
    
                status.append("MISSING")
                tags.append("missing")
    
            elif modified:
    
                status.append("OUT OF SYNC")
                tags.append("modified")
    
            else:
    
                status.append("PATCHED")
                tags.append("ok")
    
            self.files_tree.insert(
    
                file_root_nodes[parent_folder],
    
                "end",
    
                text=path.name,
    
                values=[
                    ", ".join(status),
                    str(path),
                ],
    
                tags=tags,
            )
    def apply_reference_snippets_model(
        self,
        snippets_model,
    ):
    
        self.snippet_tree.delete(
            *self.snippet_tree.get_children()
        )
    
        snippet_roots = {}
    
        grouped = defaultdict(list)
    
        for entry in snippets_model:

            operation = entry.get(
                "operation",
                "snippet",
            )
    
            grouped[
                (
                    operation,
                    entry["marker"],
                )
            ].append(entry)
    
        operation_order = {
            "snippet": 0,
            "import": 1,
            "removal": 2,
        }

        def sort_key(item):

            operation, marker = item

            return (
                operation_order.get(
                    operation,
                    99,
                ),
                str(marker).lower(),
            )

        for operation, marker in sorted(
            grouped.keys(),
            key=sort_key,
        ):
    
            entries = grouped[
                (
                    operation,
                    marker,
                )
            ]

            refs = [
                entry for entry in entries
                if not entry.get("source_only")
            ]

            source_entry = next(
                (
                    entry for entry in entries
                    if entry.get("source_file")
                ),
                entries[0],
            )
    
            source_file = source_entry.get(
                "source_file"
            )
    
            disabled = source_entry.get(
                "disabled",
                False,
            )
    
            has_file = source_file is not None
    
            root_tags = []

            if operation != "snippet":

                if refs:

                    root_tags.append("ok")

                else:

                    root_tags.append("unused")
    
            elif not has_file:
    
                root_tags.append("missing")
    
            elif disabled:
    
                root_tags.append("disabled")
    
            elif not refs:
    
                root_tags.append("unused")
    
            else:
    
                root_tags.append("ok")
    
            if operation == "import":

                icon = "📥"

                display_marker = (
                    f"MIMPORT: {marker} "
                    f"({len(refs)})"
                )

            elif operation == "removal":

                icon = "✂"

                display_marker = (
                    f"MREMOVE blocks ({len(refs)})"
                )

            else:

                icon = (
                    "⚡"
                    if has_file
                    else "🔌"
                )
    
                display_marker = (
                    f"{marker} ({len(refs)})"
                )
    
            root = self.snippet_tree.insert(
    
                "",
    
                "end",
    
                text=f"{icon} {display_marker}",
    
                open=True,
    
                values=(
    
                    str(source_file)
                    if source_file else "",
    
                    "",
    
                    str(source_file)
                    if source_file else "",
    
                    "",
                ),
    
                tags=root_tags,
            )
    
            snippet_roots[marker] = root
    
            for ref in refs:
    
                row_tags = []
    
                if operation != "snippet":

                    if ref["applied"]:

                        row_tags.append("ok")

                    else:

                        row_tags.append("modified")

                elif disabled:
    
                    row_tags.append("disabled")
    
                elif ref["applied"]:
    
                    row_tags.append("ok")
    
                else:
    
                    row_tags.append("missing")
    
                if operation == "import":

                    state = (
                        "MIMPORT_APPLIED"
                        if ref["applied"]
                        else "MIMPORT"
                    )

                elif operation == "removal":

                    state = (
                        "MREMOVE_APPLIED"
                        if ref["applied"]
                        else "MREMOVE"
                    )

                else:

                    state = (
        
                        "MSNIPPET_APPLIED"
        
                        if ref["applied"]
        
                        else "MSNIPPET"
                    )

                target_label = ref["relative"]

                if operation != "snippet":

                    display = ref.get(
                        "display",
                        "",
                    )

                    if display:

                        target_label = (
                            f"{ref['relative']} :: "
                            f"{display}"
                        )
    
                self.snippet_tree.insert(
    
                    root,
    
                    "end",
    
                    text=Path(
                        ref["workspace_file"]
                    ).name,
    
                    values=(
    
                        state,
    
                        target_label,
    
                        str(source_file)
                        if source_file else "",
    
                        str(
                            ref["workspace_file"]
                        ),
                    ),
    
                    tags=tuple(row_tags),
                )
    def apply_reference_headers_model(
        self,
        headers_model,
    ):
    
        self.header_reference_tree.delete(
            *self.header_reference_tree.get_children()
        )
    
        header_roots = {}
    
        grouped = defaultdict(list)
    
        for entry in headers_model:
    
            grouped[
                entry["marker"]
            ].append(entry)
    
        for marker in sorted(grouped.keys()):
    
            entries = grouped[marker]

            refs = [
                entry for entry in entries
                if not entry.get("source_only")
            ]

            source_entry = next(
                (
                    entry for entry in entries
                    if entry.get("source_file")
                ),
                entries[0],
            )
    
            source_file = source_entry.get(
                "source_file"
            )
    
            disabled = source_entry.get(
                "disabled",
                False,
            )
    
            has_file = source_file is not None
    
            root_tags = []
    
            if not has_file:
    
                root_tags.append("missing")
    
            elif disabled:
    
                root_tags.append("disabled")
    
            elif not refs:
    
                root_tags.append("unused")
    
            else:
    
                root_tags.append("ok")
    
            display_marker = (
                f"{marker} ({len(refs)})"
            )
    
            root = (
                self.header_reference_tree.insert(
    
                    "",
    
                    "end",
    
                    text=f"🗣️ {display_marker}",
    
                    open=True,
    
                    values=(
    
                        str(source_file)
                        if source_file else "",
    
                        "",
    
                        str(source_file)
                        if source_file else "",
    
                        "",
                    ),
    
                    tags=root_tags,
                )
            )
    
            header_roots[marker] = root
    
            for ref in refs:
    
                row_tags = []
    
                if disabled:
    
                    row_tags.append("disabled")
    
                elif ref["applied"]:
    
                    row_tags.append("ok")
    
                else:
    
                    row_tags.append("missing")
    
                state = (
    
                    "MHEADER_APPLIED"
    
                    if ref["applied"]
    
                    else "MHEADER"
                )
    
                self.header_reference_tree.insert(
    
                    root,
    
                    "end",
    
                    text=Path(
                        ref["workspace_file"]
                    ).name,
    
                    values=(
    
                        state,
    
                        ref["relative"],
    
                        str(source_file)
                        if source_file else "",
    
                        str(
                            ref["workspace_file"]
                        ),
                    ),
    
                    tags=tuple(row_tags),
                )
    def apply_reference_advanced_model(
        self,
        advanced_model,
    ):
    
        self.advanced_tree.delete(
            *self.advanced_tree.get_children()
        )
    
        advanced_roots = {}
    
        grouped = defaultdict(list)
    
        for entry in advanced_model:
    
            grouped[
                entry.get(
                    "rule_file",
                    "UNKNOWN",
                )
            ].append(entry)
    
        for rule_file, entries in sorted(
            grouped.items(),
            key=lambda x: x[0].lower(),
        ):

            if not rule_file:
                continue
    
            try:
    
                rule_path = Path(rule_file)
    
            except Exception:
    
                continue

            source_entries = [
                entry for entry in entries
                if entry.get("source_only")
            ]

            history_entries = [
                entry for entry in entries
                if not entry.get("source_only")
            ]

            source_entry = (
                source_entries[0]
                if source_entries
                else {}
            )

            scope = source_entry.get(
                "scope",
                "history",
            )

            enabled = source_entry.get(
                "enabled",
                True,
            )

            history_statuses = [
                entry.get("status", "UNKNOWN")
                for entry in history_entries
            ]

            root_tags = []

            if source_entry and not enabled:

                root_tags.append("disabled")

            elif "MISSING" in history_statuses:

                root_tags.append("missing")

            elif any(
                status in (
                    "STALE",
                    "RULE CHANGED",
                    "ERROR",
                )
                for status in history_statuses
            ):

                root_tags.append("modified")

            elif history_entries:

                root_tags.append("ok")

            elif source_entry:

                root_tags.append("unused")

            elif not rule_path.exists():

                root_tags.append("missing")

            else:

                root_tags.append("unused")
    
            display_name = (
                f"{rule_path.name} "
                f"[{scope}] "
                f"({len(history_entries)})"
            )
    
            root = self.advanced_tree.insert(
    
                "",
    
                "end",
    
                text=f"💉 {display_name}",
    
                open=True,
    
                values=(
    
                    "",
    
                    "",
    
                    "",
    
                    str(rule_path),
                ),
    
                tags=tuple(root_tags),
            )
    
            advanced_roots[
                rule_file
            ] = root
    
            for entry in sorted(
    
                history_entries,
    
                key=lambda x: x.get(
                    "timestamp",
                    0,
                ),
    
                reverse=True,
            ):
    
                target = Path(
                    entry.get(
                        "target",
                        "",
                    )
                )
    
                relative = entry.get(
                    "relative_target",
                    target.name,
                )
    
                timestamp = entry.get(
                    "timestamp",
                    0,
                )
    
                formatted_time = time.strftime(
    
                    "%Y-%m-%d %H:%M",
    
                    time.localtime(timestamp),
                )
    
                status = entry.get(
                    "status",
                    "UNKNOWN",
                )
    
                tags = ("ok",)
    
                if status in (
                    "STALE",
                    "RULE CHANGED",
                    "ERROR",
                ):
    
                    tags = ("modified",)
    
                elif status == "MISSING":
    
                    tags = ("missing",)
    
                self.advanced_tree.insert(
    
                    root,
    
                    "end",
    
                    text=target.name,
    
                    values=(
    
                        status,
    
                        relative,
    
                        formatted_time,
    
                        str(target),
                    ),
    
                    tags=tags,
                )
    
    
    def schedule_refresh(
        self,
        scope=REFRESH_MINIMAL,
        changed_paths=None,
    ):
    
        # =====================================
        # UPGRADE SCOPE
        # =====================================
    
        current_priority = REFRESH_PRIORITY.get(
            self.pending_refresh_scope,
            0,
        )
    
        new_priority = REFRESH_PRIORITY.get(
            scope,
            0,
        )
    
        if new_priority > current_priority:
    
            self.pending_refresh_scope = scope
    
        # =====================================
        # TRACK CHANGED PATHS
        # =====================================
    
        if changed_paths:
    
            for path in changed_paths:
    
                try:
    
                    self.pending_changed_paths.add(
                        str(path)
                    )
    
                except Exception:
                    pass
    
        # =====================================
        # DEBOUNCE
        # =====================================
    
        if self.refresh_after_id:
    
            try:
    
                self.root.after_cancel(
                    self.refresh_after_id
                )
    
            except Exception:
                pass
    
        self.refresh_after_id = self.root.after(
            120,
            self.begin_refresh,
        )
    def begin_refresh(self):
    
        self.refresh_after_id = None
    
        if self.refresh_in_progress:
    
            self.refresh_pending = True
            return
    
        self.refresh_in_progress = True
    
        scope = self.pending_refresh_scope
        
        self.loading_var.set(
            "Refreshing..."
        )
    
        changed_paths = set(
            self.pending_changed_paths
        )
    
        # =====================================
        # RESET PENDING STATE
        # =====================================
    
        self.pending_refresh_scope = (
            REFRESH_MINIMAL
        )
    
        self.pending_changed_paths.clear()
    
        generation = (
            self.refresh_generation + 1
        )
    
        self.refresh_generation = generation
    
        future = (
            self.background_executor.submit(
                self.build_refresh_payload,
                generation,
                scope,
                changed_paths,
                id(self.engine),
            )
        )
    
        future.add_done_callback(
            lambda f: self.root.after(
                0,
                self.finish_refresh,
                f,
            )
        )
    def build_refresh_payload(
        self,
        generation,
        scope,
        changed_paths,
        engine_id,
    ):
    
        payload = {
    
            "generation": generation,
    
            "scope": scope,
    
            "changed_paths": changed_paths,

            "engine_id": engine_id,
        }
    
        # =====================================
        # FULL REFRESH
        # =====================================
    
        if scope == REFRESH_FULL:

            self.engine.sync_patch_tree()

            payload["workspace_search_cache"] = (
                self.engine.build_search_cache(
                    self.engine.workspace_root
                )
            )

            payload["patch_search_cache"] = (
                self.engine.build_search_cache(
                    self.engine.patch_root
                )
            )
    
            payload["workspace_model"] = (
                self.tree_manager.build_tree_model(
                    self.engine.workspace_root,
                    glow=True,
                )
            )
    
            payload["patch_model"] = (
                self.tree_manager.build_tree_model(
                    self.engine.patch_root,
                    glow=False,
                )
            )
    
            payload["reference_model"] = (
                self.build_reference_model()
            )
    
        # =====================================
        # PARTIAL REFRESH
        # =====================================
    
        elif scope == REFRESH_PARTIAL:
    
            payload["reference_model"] = (
                self.build_reference_model()
            )
    
        return payload
    def finish_refresh(
        self,
        future,
    ):
    
        try:
    
            payload = future.result()
    
        except Exception as e:
        
            self.log.log(
                f"Refresh failed: {e}"
            )
        
            self.loading_var.set("")
        
            self.refresh_in_progress = False
            return
    
        generation = payload["generation"]
    
        if (
            generation != self.refresh_generation
            or payload.get("engine_id") != id(self.engine)
        ):
        
            self.loading_var.set("")
        
            self.refresh_in_progress = False
            return
    
        scope = payload["scope"]
    
        try:
    
            self.is_refreshing_trees = True
    
            # =====================================
            # FULL REFRESH
            # =====================================
    
            if scope == REFRESH_FULL:

                self.engine.workspace_search_cache = (
                    payload.get(
                        "workspace_search_cache",
                        [],
                    )
                )

                self.engine.patch_search_cache = (
                    payload.get(
                        "patch_search_cache",
                        [],
                    )
                )

                workspace_search = (
                    self.workspace_search.get().strip()
                )

                patch_search = (
                    self.patch_search.get().strip()
                )

                if workspace_search:

                    self.refresh_workspace_tree_only()

                else:

                    self.apply_tree_model_chunked(
                        self.workspace_tree,
                        payload["workspace_model"],
                        self.state.workspace_node_map,
                    )

                if patch_search:

                    self.refresh_patch_tree_only()

                else:

                    self.apply_tree_model_chunked(
                        self.patch_tree,
                        payload["patch_model"],
                        self.state.patch_node_map,
                    )
    
                self.apply_reference_model(
                    payload["reference_model"]
                )
    
            # =====================================
            # PARTIAL REFRESH
            # =====================================
    
            elif scope == REFRESH_PARTIAL:
    
                self.apply_reference_model(
                    payload["reference_model"]
                )
    
        finally:
    
            self.is_refreshing_trees = False
    
            self.refresh_in_progress = False
    
        # =====================================
        # PENDING REFRESH
        # =====================================
    
        if self.refresh_pending:
        
            self.refresh_pending = False
        
            self.begin_refresh()
        
        else:
        
            self.loading_var.set("")
    def refresh_trees(self):

        self.check_workspace_identity()
    
        self.schedule_refresh(
            scope=REFRESH_FULL
        )

    def process_loop(self):
    
        if self.is_shutting_down:
            return
    
        try:
    
            self.process_activity_queue()
    
            self.process_pending_patches()
    
        except Exception as e:
    
            self.log.log(
                f"Main loop error: {e}"
            )
    
        if self.is_shutting_down:
            return
    
        try:
    
            self.loop_after_id = self.root.after(
                100,
                self.process_loop,
            )
    
        except tk.TclError:
            pass

    def process_activity_queue(self):
    
        updated = False
    
        while not self.activity_queue.empty():
    
            path = self.activity_queue.get()
    
            if path in self.state.recent_activity:
    
                self.state.recent_activity.remove(path)
    
            self.state.recent_activity.append(path)
    
            self.state.recent_activity = (
                self.state.recent_activity[-100:]
            )
    
            self.activity_list.insert(
                tk.END,
                path,
            )
    
            updated = True
    
        # =====================================
        # LIMIT VISUAL LIST SIZE
        # =====================================
    
        while self.activity_list.size() > 100:
    
            self.activity_list.delete(0)
    
        # =====================================
        # AUTO SCROLL TO BOTTOM
        # =====================================
    
        if updated:
    
            self.activity_list.see(tk.END)
    def process_pending_patches(self):
    
        if not self.patching_enabled.get():
            return
    
        if not self.engine:
            return
    
        quiet = (
            time.time() -
            self.last_event_time
        )
    
        if quiet < QUIET_TIME_SECONDS:
    
            return
    
        with self.lock:
    
            if not self.state.pending_files:
    
                return

            patch_keys = list(
                self.state.pending_files.keys()
            )[:MAX_PATCHES_PER_LOOP]

            patches = [
                self.state.pending_files.pop(key)
                for key in patch_keys
                if key in self.state.pending_files
            ]

            remaining_count = len(
                self.state.pending_files
            )

        if not patches:
            return

        self.log.log(
            (
                f"[PROCESS] Pending patches: "
                f"{len(patches)}"
                f" ({remaining_count} remaining)"
            )
        )
        for patch in patches:
            self.log.log(
                f"[PROCESS] Patching: "
                f"{patch.changed_file} <- {patch.override_path}"
            )
            self.engine.patch_file(
                patch.changed_file,
                patch.override_path,
            )

            if patch.event_type == "OUT_OF_SYNC":

                try:

                    patch_hash = (
                        self.engine.compute_file_hash(
                            patch.override_path
                        )
                    )

                    workspace_hash = (
                        self.engine.compute_file_hash(
                            patch.changed_file
                        )
                    )

                    if (
                        patch_hash is not None
                        and workspace_hash is not None
                    ):

                        self.state.out_of_sync_attempts[
                            str(patch.override_path)
                        ] = (
                            f"{patch_hash}:"
                            f"{workspace_hash}"
                        )

                except Exception:

                    pass
    
        changed_paths = []
        
        for patch in patches:
        
            changed_paths.append(
                patch.changed_file
            )
        
            changed_paths.append(
                patch.override_path
            )
        
        self.schedule_refresh(
            scope=REFRESH_FULL,
            changed_paths=changed_paths,
        )
    
    # =====================================================
    # ERRORS
    # =====================================================

    def tk_exception(self, exc, val, tb):

        error = "".join(
            traceback.format_exception(
                exc,
                val,
                tb,
            )
        )

        self.log.log(error)

    # =====================================================
    # SHUTDOWN
    # =====================================================

    def shutdown(self):
    
        if self.is_shutting_down:
            return
    
        self.is_shutting_down = True
    
        # =========================================
        # CANCEL AFTER LOOP
        # =========================================
    
        if self.loop_after_id is not None:
    
            try:
    
                self.root.after_cancel(
                    self.loop_after_id
                )
    
            except Exception:
                pass
    
            self.loop_after_id = None
    
        # =========================================
        # STOP OBSERVER
        # =========================================
    
        try:
    
            if (
                self.observer and
                self.observer.is_alive()
            ):
    
                self.observer.stop()
    
                self.observer.join(timeout=2)
    
        except Exception as e:
    
            self.log.log(
                f"Observer shutdown failed: {e}"
            )
    
        # =========================================
        # DESTROY TK SAFELY
        # =========================================
    
        try:
    
            self.root.withdraw()
    
        except Exception:
            pass
    
        try:
    
            self.root.quit()
    
        except Exception:
            pass
    
        # IMPORTANT:
        # Delay destroy slightly so Tcl can flush
        # pending events cleanly.
        try:
    
            self.root.after(
                50,
                self.root.destroy,
            )
    
        except Exception:
            try:
                self.root.destroy()
            except Exception:
                pass

    # =====================================================
    # RUN
    # =====================================================

    def run(self):
    
        self.root.mainloop()


# =========================================================
# ENTRYPOINT
# =========================================================

if __name__ == "__main__":

    app = MCodePatcherApp()

    app.run()
