#!/usr/bin/env python3
"""Refresh tracked code without replacing the workspace or touching runtime data.

Stop the gateway before running: individual files are replaced atomically, but
an entire release is not. Removed source files are retained for manual review.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def sync(source: Path, target: Path):
    source = source.resolve()
    if target.is_symlink():
        raise ValueError('Workspace must not be a symlink')
    target = target.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError('Source and workspace must be independent directories')
    files = subprocess.check_output(['git', '-C', str(source), 'ls-files', '-z']).decode().split('\0')
    selected = []
    for name in filter(None, files):
        rel = Path(name)
        if any(p in {'data', '.git', '.venv', '__pycache__'} or p.startswith('.env') for p in rel.parts):
            continue
        src, dst = source / rel, target / rel
        if src.is_symlink() or not src.is_file():
            raise ValueError('Tracked source must be a regular file')
        if any(p.is_symlink() for p in [dst, *dst.parents]):
            raise ValueError('Destination must not traverse symlinks')
        selected.append((src, dst))
    for src, dst in selected:
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.travel-sync-', dir=dst.parent)
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(src.read_bytes())
            os.chmod(temporary, src.stat().st_mode & 0o777)
            os.replace(temporary, dst)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return len(selected)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('target', type=Path)
    args = parser.parse_args()
    print(f'Updated {sync(args.source, args.target)} code files; runtime files retained.')
