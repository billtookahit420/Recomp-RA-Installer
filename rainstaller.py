#!/usr/bin/env python3
"""Recomp RA Installer - core logic (UI-free).

Installs, verifies and strips RetroAchievements config
(retroachievements.toml) across a whole library of extracted recomp
packages, from ONE master copy the user picks. The strip pass exists
for home-server uploads (the toml carries the user's login token);
the install/verify passes put it back and catch stale copies.

Hard rules (settled with Duane, 2026-11-11):
  * Token hygiene is absolute. The api_token VALUE is never printed,
    logged, reported or embedded anywhere - comparisons happen by
    parsed-field equality in memory, and every message names FIELDS
    only ("api_token: mismatch"), never values.
  * No master ships with the tool. The user picks their own master
    retroachievements.toml; the tool only ever copies that file.
  * A package whose toml differs from the master is STALE. It is
    never overwritten unless the user explicitly ticks that row or
    explicitly runs "update all stale" after seeing the count.
  * Installs are atomic: write to a temp file in the same folder,
    then os.replace() into place, then re-read and verify. An
    interrupted run never leaves a torn toml.
  * Scanning is listing-first: it reads only the small text configs
    that decide placement and state (game.toml for the title,
    retroachievements.toml for the field comparison). It never reads
    a game executable, so a ~750-package library scans fast - the
    ROM Installer's v1.0.1 lesson (a hashing scan took 52 s where a
    listing took 0.02 s) is not repeated here.

Package ground truth (inspected 2026-11-11 from shipped packages):
  * A package unit is a folder containing game.toml, or a shipped
    retroachievements.toml.example. The filled toml belongs in that
    SAME folder - that is where every shipped package keeps it.
  * PS1 (PSXRecomp): one folder per game - game.toml, the game
    executable and retroachievements.toml side by side at the
    package root (Doom / Tekken 2 shipped zips and fresh extracts).
    A multi-disc set is still one package folder, one toml.
  * GBA (GBARecomp Tools): the package wrapper holds per-OS payload
    folders (linux/ and windows/); EACH payload folder carries its
    own game.toml and its own retroachievements.toml (Super Mario
    Advance shipped zip). The marker rule covers this unchanged:
    each payload folder is its own unit.
  * NES / GB / SNES shipped packages contain NO retroachievements
    .toml at all (checked in the family's evidence zips), so v1 does
    not guess a location for them - they are outside the serviced
    scope until a shipped package proves where their config lives.

Cross-platform: pathlib throughout, os.replace for the atomic
rename, no POSIX-only calls.
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "Recomp RA Installer"
TOML_NAME = "retroachievements.toml"
EXAMPLE_NAME = "retroachievements.toml.example"
GAME_TOML_NAME = "game.toml"
TEMP_PREFIX = ".recomp-ra-installer-"
TEMP_SUFFIX = ".tmp"
FIELDS = ("username", "api_token", "hardcore")
MAX_CONFIG_BYTES = 1 << 18          # 256 KB cap on any config we read
SETTINGS_NAME = "settings.json"

_PS1_ID = re.compile(r"^(SLUS|SCUS|SLES|SCES|SLPS|SCPS|SLPM|SCU|SLU)")
_STR_FIELD = {
    name: re.compile(r'^\s*' + name + r'\s*=\s*"([^"]*)"', re.M)
    for name in ("username", "api_token")
}
_HARDCORE = re.compile(r"^\s*hardcore\s*=\s*(true|false)", re.M | re.I)
_GAME_NAME = re.compile(r'^\s*name\s*=\s*"([^"]*)"', re.M)
_GAME_ID = re.compile(r'^\s*id\s*=\s*"([^"]*)"', re.M)


class Cancelled(Exception):
    pass


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise Cancelled("cancelled")


# ----------------------------------------------------------------------
# retroachievements.toml parsing (field values stay in memory only)
# ----------------------------------------------------------------------
def parse_ra_fields(text):
    """Pull the three login fields out of a retroachievements.toml.

    Returns {field: value-or-None}. Values are compared by callers
    and must never be written to a log or report.
    """
    out = {}
    for name, rx in _STR_FIELD.items():
        m = rx.search(text)
        out[name] = m.group(1) if m else None
    m = _HARDCORE.search(text)
    out["hardcore"] = (m.group(1).lower() == "true") if m else None
    return out


@dataclass
class MasterRA:
    path: Path
    raw: bytes
    fields: dict

    def describe(self):
        """A log-safe summary: field presence and the hardcore flag,
        never a value. (The username is the user's own public handle
        and is already printed on every package he owns; the token is
        not shown anywhere, ever.)"""
        return (f"master {self.path.name}: "
                f"username={self.fields.get('username') or '(missing)'}, "
                f"api_token={'present' if self.fields.get('api_token') else 'MISSING'}, "
                f"hardcore={self.fields.get('hardcore')}")


def load_master(path):
    """Read and validate the user's master toml. Raises ValueError
    naming the offending FIELD (never a value) when it is unusable."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise ValueError(f"cannot read master file: {e}") from e
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("master file is implausibly large for a "
                         "retroachievements.toml")
    fields = parse_ra_fields(raw.decode("utf-8", errors="replace"))
    missing = [f for f in FIELDS if fields.get(f) in (None, "")]
    if missing:
        raise ValueError("master is missing or has an empty field: "
                         + ", ".join(missing))
    return MasterRA(path=p, raw=raw, fields=fields)


def differing_fields(master_fields, candidate_fields):
    """Names of the login fields that differ (missing counts as
    differing). Field NAMES only - safe for logs and reports."""
    return [f for f in FIELDS
            if candidate_fields.get(f) != master_fields.get(f)]


# ----------------------------------------------------------------------
# Package scanning
# ----------------------------------------------------------------------
@dataclass
class RAUnit:
    folder: Path
    title: str
    system: str
    state: str = "missing"      # good / stale / missing / error
    differ: list = field(default_factory=list)   # field NAMES only
    detail: str = ""

    @property
    def key(self):
        return str(self.folder)

    @property
    def toml_path(self):
        return self.folder / TOML_NAME

    def state_text(self):
        if self.state == "good":
            return "installed, matches master"
        if self.state == "stale":
            return "stale: differs in " + ", ".join(self.differ)
        if self.state == "missing":
            return "missing"
        return f"error: {self.detail}"


def _read_small(path):
    with open(path, "rb") as f:
        return f.read(MAX_CONFIG_BYTES).decode("utf-8", errors="replace")


def _game_info(folder):
    """(title, system) from the package's own game.toml, when it has
    one. Display only - placement never depends on this."""
    gt = folder / GAME_TOML_NAME
    if not gt.is_file():
        return folder.name, "Recomp package"
    try:
        text = _read_small(gt)
    except OSError:
        return folder.name, "Recomp package"
    m = _GAME_NAME.search(text)
    title = m.group(1) if m else folder.name
    mid = _GAME_ID.search(text)
    gid = mid.group(1) if mid else ""
    if _PS1_ID.match(gid):
        system = "PlayStation"
    elif "[identity]" in text and "[program]" in text:
        system = "Game Boy Advance"
    else:
        system = "Recomp package"
    return title, system


def _classify(folder, master):
    """Build the RAUnit for one marker folder and grade its toml."""
    title, system = _game_info(folder)
    unit = RAUnit(folder=folder, title=title, system=system)
    toml = unit.toml_path
    if not toml.is_file():
        unit.state = "missing"
        unit.detail = "no retroachievements.toml"
        return unit
    try:
        fields = parse_ra_fields(_read_small(toml))
    except OSError as e:
        unit.state = "error"
        unit.detail = f"toml present but unreadable: {e}"
        return unit
    if all(fields.get(f) is None for f in FIELDS):
        unit.state = "stale"
        unit.differ = list(FIELDS)
        unit.detail = "toml present but no login fields could be parsed"
        return unit
    diff = differing_fields(master.fields, fields)
    if diff:
        unit.state = "stale"
        unit.differ = diff
        unit.detail = "differs in " + ", ".join(diff)
    else:
        unit.state = "good"
        unit.detail = "all fields match master"
    return unit


def scan_library(root, master, cancel=None, progress=None):
    """Walk a library root and grade every package unit in it.

    Returns (units, skipped_count, notes). A folder is a unit when it
    contains game.toml or a shipped retroachievements.toml.example;
    units are not descended into (their subfolders are package
    internals, and per-OS payload folders are reached because the
    wrapper above them is not itself a unit).
    """
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"library folder does not exist: {root}")
    units = []
    notes = []
    skipped = 0
    seen_dirs = 0

    def on_error(e):
        notes.append(f"could not list {e.filename}: {e.strerror or e}")

    for dirpath, dirnames, filenames in os.walk(
            root, topdown=True, onerror=on_error, followlinks=False):
        _check_cancel(cancel)
        seen_dirs += 1
        if progress is not None and seen_dirs % 100 == 0:
            progress(f"scanning… {seen_dirs} folders, "
                     f"{len(units)} packages")
        names = set(filenames)
        folder = Path(dirpath)
        if GAME_TOML_NAME in names or EXAMPLE_NAME in names:
            units.append(_classify(folder, master))
            dirnames[:] = []            # package internals: stop here
        elif names:
            skipped += 1                # a folder with files, no marker
    units.sort(key=lambda u: u.key.lower())
    return units, skipped, notes


# ----------------------------------------------------------------------
# Install / strip passes
# ----------------------------------------------------------------------
def _atomic_install(unit, master):
    """Write the master bytes beside the game, atomically, then
    re-read and verify the installed file parses to the master's
    fields. Raises OSError/ValueError on failure."""
    tmp = unit.folder / (TEMP_PREFIX + str(os.getpid()) + TEMP_SUFFIX)
    try:
        with open(tmp, "wb") as f:
            f.write(master.raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, unit.toml_path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    fields = parse_ra_fields(_read_small(unit.toml_path))
    if differing_fields(master.fields, fields):
        raise ValueError("installed file failed verification")


def run_install(units, master, update_keys=frozenset(), cancel=None,
                progress=None):
    """Install into MISSING units; update STALE units only when their
    key is in update_keys. Returns a list of per-unit result dicts
    {unit, action, detail} with action in installed / updated /
    skipped / error. Unit states are updated in place."""
    results = []
    total = len(units)
    for i, unit in enumerate(units):
        _check_cancel(cancel)
        if progress is not None:
            progress(f"installing… {i + 1}/{total}: {unit.title}")
        if unit.state == "good":
            results.append({"unit": unit, "action": "skipped",
                            "detail": "already matches master"})
            continue
        if unit.state == "error":
            results.append({"unit": unit, "action": "skipped",
                            "detail": unit.detail})
            continue
        if unit.state == "stale" and unit.key not in update_keys:
            results.append({"unit": unit, "action": "skipped",
                            "detail": "stale, not ticked for update"})
            continue
        was = unit.state
        try:
            _atomic_install(unit, master)
        except (OSError, ValueError) as e:
            results.append({"unit": unit, "action": "error",
                            "detail": str(e)})
            continue
        unit.state = "good"
        unit.differ = []
        unit.detail = "all fields match master"
        results.append({"unit": unit,
                        "action": "installed" if was == "missing"
                        else "updated",
                        "detail": "written and verified"})
    return results


def run_strip(units, cancel=None, progress=None):
    """Remove retroachievements.toml from every unit that has one.
    Returns per-unit result dicts (removed / skipped / error). A run
    with nothing present removes nothing and writes nothing."""
    results = []
    total = len(units)
    for i, unit in enumerate(units):
        _check_cancel(cancel)
        if progress is not None:
            progress(f"stripping… {i + 1}/{total}: {unit.title}")
        if not unit.toml_path.is_file():
            results.append({"unit": unit, "action": "skipped",
                            "detail": "already absent"})
            continue
        try:
            unit.toml_path.unlink()
        except OSError as e:
            results.append({"unit": unit, "action": "error",
                            "detail": str(e)})
            continue
        unit.state = "missing"
        unit.differ = []
        unit.detail = "no retroachievements.toml"
        results.append({"unit": unit, "action": "removed",
                        "detail": "toml removed"})
    return results


def count_states(units):
    counts = {"good": 0, "stale": 0, "missing": 0, "error": 0}
    for u in units:
        counts[u.state] = counts.get(u.state, 0) + 1
    return counts


# ----------------------------------------------------------------------
# Report + settings
# ----------------------------------------------------------------------
def make_report(units, skipped, notes, master, root, actions=None):
    counts = count_states(units)
    lines = [
        f"# {APP_NAME} report",
        "",
        f"Library: {root}",
        f"Master: {master.describe()}" if master else "Master: (none)",
        f"Packages: {len(units)} "
        f"(good {counts['good']}, stale {counts['stale']}, "
        f"missing {counts['missing']}, error {counts['error']}); "
        f"folders skipped (no package marker): {skipped}",
        "",
    ]
    if actions:
        lines.append("## Actions")
        lines.append("")
        for a in actions:
            lines.append(f"- {a['unit'].title}: {a['action']} - "
                         f"{a['detail']}")
        lines.append("")
    lines.append("## Packages")
    lines.append("")
    for u in units:
        lines.append(f"- {u.title} [{u.system}] - {u.state_text()} "
                     f"({u.folder})")
    if notes:
        lines.append("")
        lines.append("## Notes")
        lines.append("")
        for n in notes:
            lines.append(f"- {n}")
    return "\n".join(lines) + "\n"


def settings_path():
    return Path(__file__).resolve().parent / SETTINGS_NAME


def load_settings():
    try:
        data = json.loads(settings_path().read_text(errors="replace"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    p = settings_path()
    tmp = p.with_name(TEMP_PREFIX + "settings" + TEMP_SUFFIX)
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, p)
