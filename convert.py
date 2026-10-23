#!/usr/bin/env python3
"""Convert books in a library folder to extra formats, saved next to the original.

Library apps that treat same-named files in one folder as one book in several formats
(Grimmory, BookOrbit, ...) pick up the new file as an extra format of the existing book.
"""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# Temporary files are hidden (leading dot) and end in .part, which library apps ignore.
PART_SUFFIX = ".ebook-convert.part"


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def env_list(name, default):
    return [f.strip().lower().lstrip(".") for f in os.environ.get(name, default).split(",") if f.strip()]


def env_bool(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    library_dir: Path
    source_formats: list
    target_formats: list
    dry_run: bool
    interval_minutes: int
    max_per_run: int
    timeout: int
    state_dir: Path
    ebook_convert: list
    extra_args: list
    plugins_dir: Path
    calibre_customize: list
    ignore_files: list

    @classmethod
    def from_env(cls):
        return cls(
            library_dir=Path(os.environ.get("LIBRARY_DIR", "/books")),
            source_formats=env_list("SOURCE_FORMATS", "epub"),
            target_formats=env_list("TARGET_FORMATS", "azw3"),
            dry_run=env_bool("DRY_RUN", "true"),
            interval_minutes=int(os.environ.get("INTERVAL_MINUTES", "0")),
            max_per_run=int(os.environ.get("MAX_PER_RUN", "0")),
            timeout=int(os.environ.get("CONVERT_TIMEOUT", "600")),
            state_dir=Path(os.environ.get("STATE_DIR", "/config")),
            ebook_convert=shlex.split(os.environ.get("EBOOK_CONVERT", "ebook-convert")),
            extra_args=shlex.split(os.environ.get("EXTRA_ARGS", "")),
            plugins_dir=Path(os.environ.get("PLUGINS_DIR", "/plugins")),
            calibre_customize=shlex.split(os.environ.get("CALIBRE_CUSTOMIZE", "calibre-customize")),
            ignore_files=[f.strip() for f in os.environ.get("IGNORE_FILES", ".ignore").split(",") if f.strip()],
        )


def split_ext(name):
    stem, dot, ext = name.rpartition(".")
    return (stem, ext.lower()) if dot and stem else (name, "")


def fingerprint(path):
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


def rel(cfg, path):
    return str(path.relative_to(cfg.library_dir))


def find_jobs(cfg, state):
    """Return (source, target, fmt, is_update) for every conversion that is missing or out of date.

    A converted file is out of date when its source changed since the conversion (library apps
    rewrite the EPUB when metadata is saved to the file). Only files this tool created are
    ever replaced; a converted file that the library app moved or renamed is recognised by its
    fingerprint. Scans the whole library before anything is converted, so state about moved
    files is complete when it gets pruned.
    """
    converted, failed = state["converted"], state["failed"]
    by_fingerprint = {tuple(e["target"]): r for r, e in converted.items()}
    jobs = []
    for dirpath, dirnames, filenames in os.walk(cfg.library_dir):
        # A folder containing one of the ignore files (Grimmory's .ignore by default) is skipped.
        if any(f in filenames for f in cfg.ignore_files):
            dirnames.clear()
            continue
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))

        files = {}  # (stem lowercased, ext) -> file name
        sources = {}  # stem lowercased -> file name of the preferred source
        for name in sorted(filenames):
            if name.startswith(".") or name.endswith(PART_SUFFIX):
                continue
            stem, ext = split_ext(name)
            files[(stem.lower(), ext)] = name
            if ext in cfg.source_formats:
                current = sources.get(stem.lower())
                if current is None or cfg.source_formats.index(ext) < cfg.source_formats.index(split_ext(current)[1]):
                    sources[stem.lower()] = name

        for key, name in sorted(sources.items()):
            source = Path(dirpath) / name
            try:
                source_fp = fingerprint(source)
                stem = split_ext(name)[0]
                for fmt in cfg.target_formats:
                    existing = files.get((key, fmt))
                    target = Path(dirpath) / existing if existing else source.with_name(f"{stem}.{fmt}")
                    target_rel = rel(cfg, target)
                    if existing:
                        target_fp = fingerprint(target)
                        entry = converted.get(target_rel)
                        if entry is None:
                            old = by_fingerprint.get(tuple(target_fp))
                            if old is None or (cfg.library_dir / old).exists():
                                continue  # not created by this tool: never touch it
                            entry = converted[target_rel] = converted.pop(old)
                        entry["target"] = target_fp
                        if entry["source"] == source_fp:
                            continue
                    fail = failed.get(target_rel)
                    if fail and fail["source"] == source_fp:
                        continue  # failed before and the source has not changed since
                    jobs.append((source, target, fmt, bool(existing)))
            except OSError as e:
                # E.g. the library app moved or deleted the file mid-scan; the next run sees the new state.
                log(f"Skipping {source}: {e}")

    for r in [r for r in converted if not (cfg.library_dir / r).exists()]:
        del converted[r]
    return jobs


def load_state(cfg):
    try:
        state = json.loads((cfg.state_dir / "state.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("converted", {})
    state.setdefault("failed", {})
    return state


def save_state(cfg, state):
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    tmp = cfg.state_dir / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(cfg.state_dir / "state.json")


class DRMProtected(RuntimeError):
    """Calibre refuses to convert the book because it is DRM-protected."""


def convert(cfg, source, target, fmt):
    """Convert in a scratch dir, then move into the library under a hidden name library apps
    ignore and rename it into place, so their watchers only ever see a complete file."""
    with tempfile.TemporaryDirectory(prefix="ebook-convert-") as work:
        out = Path(work) / f"out.{fmt}"
        cmd = [*cfg.ebook_convert, str(source), str(out), *cfg.extra_args]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg.timeout)
        if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            output = result.stderr or result.stdout
            if "DRMError" in output:
                raise DRMProtected("DRM-protected")
            tail = output.strip().splitlines()[-5:]
            raise RuntimeError(f"ebook-convert exited {result.returncode}: " + " | ".join(tail))
        part = part_path(target)
        # Never follow a pre-existing part file or symlink: anyone who can write to
        # the library could otherwise redirect this write elsewhere.
        part.unlink(missing_ok=True)
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
        with os.fdopen(fd, "wb") as dst, open(out, "rb") as src:
            shutil.copyfileobj(src, dst)
        os.replace(part, target)


def part_path(target):
    return target.with_name(f".{target.name}{PART_SUFFIX}")


def clean_stale_parts(cfg):
    for part in cfg.library_dir.rglob(f".*{PART_SUFFIX}"):
        log(f"Removing leftover {part}")
        part.unlink(missing_ok=True)


def plugins_signature(cfg):
    """What is in the plugins dir; a conversion that failed may work with other plugins."""
    if not cfg.plugins_dir.is_dir():
        return []
    return [[f.name, *fingerprint(f)] for f in sorted(cfg.plugins_dir.iterdir()) if f.is_file()]


def run_once(cfg):
    if not cfg.library_dir.is_dir():
        log(f"Library dir {cfg.library_dir} does not exist")
        return
    clean_stale_parts(cfg)
    state = load_state(cfg)
    plugins = plugins_signature(cfg)
    if state.get("plugins") != plugins:
        if state["failed"]:
            log(f"Calibre plugins changed, retrying {len(state['failed'])} failed conversion(s)")
            state["failed"] = {}
        state["plugins"] = plugins
    jobs = find_jobs(cfg, state)
    if not cfg.dry_run:
        save_state(cfg, state)
    done = errors = drm = 0
    for source, target, fmt, is_update in jobs:
        if cfg.max_per_run and done + errors + drm >= cfg.max_per_run:
            log(f"Reached MAX_PER_RUN={cfg.max_per_run}, rest waits for the next run")
            break
        target_rel = rel(cfg, target)
        action = "update" if is_update else "create"
        if cfg.dry_run:
            log(f"[dry-run] would {action} {target_rel}")
            done += 1
            continue
        reason = " (source changed)" if is_update else ""
        log(f"Converting {rel(cfg, source)} -> {fmt}{reason}")
        started = time.monotonic()
        try:
            # Taken before converting, so a change during the conversion triggers another one.
            source_fp = fingerprint(source)
        except OSError as e:
            log(f"Skipping {rel(cfg, source)}: {e}")
            continue
        try:
            convert(cfg, source, target, fmt)
            target_fp = fingerprint(target)
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
            state["failed"][target_rel] = {"error": str(e)[:500], "source": source_fp}
            save_state(cfg, state)
            if isinstance(e, DRMProtected):
                drm += 1
                log(f"Skipped (DRM-protected): {rel(cfg, source)}")
            else:
                errors += 1
                log(f"FAILED {target_rel}: {e}")
            continue
        state["converted"][target_rel] = {"source": source_fp, "target": target_fp}
        state["failed"].pop(target_rel, None)
        save_state(cfg, state)
        done += 1
        log(f"{'Updated' if is_update else 'Created'} {target_rel} in {time.monotonic() - started:.0f}s")
    verb = "would convert" if cfg.dry_run else "converted"
    log(f"Run finished: {verb} {done}, failed {errors}" + (f", DRM-protected {drm}" if drm else ""))


def install_plugins(cfg):
    """Install every Calibre plugin .zip in the plugins dir into a fresh Calibre config, so the
    folder is the complete list: removing a zip removes the plugin on the next start. A plugin's
    settings file (the .json Calibre keeps in its plugins folder) can be put next to the zips."""
    config_dir = os.environ.get("CALIBRE_CONFIG_DIRECTORY")
    if not config_dir or not cfg.plugins_dir.is_dir():
        return
    shutil.rmtree(config_dir, ignore_errors=True)
    for plugin in sorted(cfg.plugins_dir.glob("*.zip")):
        result = subprocess.run([*cfg.calibre_customize, "-a", str(plugin)], capture_output=True, text=True)
        if result.returncode == 0:
            log(f"Installed Calibre plugin {plugin.name}")
        else:
            tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
            log(f"FAILED to install Calibre plugin {plugin.name}: " + " | ".join(tail))
    settings = sorted(cfg.plugins_dir.glob("*.json"))
    if settings:
        target = Path(config_dir) / "plugins"
        target.mkdir(parents=True, exist_ok=True)
        for f in settings:
            shutil.copyfile(f, target / f.name)
            log(f"Copied Calibre plugin settings {f.name}")


def drop_privileges():
    """Started as root (the default), switch to PUID/PGID so converted files get the same owner
    as the rest of the library. Started as another user (e.g. docker --user), keep that user."""
    if os.getuid() != 0:
        return
    uid, gid = int(os.environ.get("PUID", "99")), int(os.environ.get("PGID", "100"))
    state_dir = Path(os.environ.get("STATE_DIR", "/config"))
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chown(state_dir, uid, gid)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def main():
    # PID 1 in a container ignores SIGTERM unless handled; exiting via SystemExit
    # also makes subprocess.run kill a running ebook-convert.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    os.umask(int(os.environ.get("UMASK", "002"), 8))
    drop_privileges()
    cfg = Config.from_env()
    install_plugins(cfg)
    log(
        f"Library {cfg.library_dir}: {','.join(cfg.source_formats)} -> {','.join(cfg.target_formats)}"
        f"{' (DRY RUN, nothing is written)' if cfg.dry_run else ''}"
    )
    while True:
        try:
            run_once(cfg)
        except Exception as e:
            if cfg.interval_minutes <= 0:
                raise
            # Keep the container alive; the next run starts from a fresh scan.
            log(f"Run failed: {e!r}")
        if cfg.interval_minutes <= 0:
            return
        log(f"Next run in {cfg.interval_minutes} min")
        time.sleep(cfg.interval_minutes * 60)


if __name__ == "__main__":
    main()
