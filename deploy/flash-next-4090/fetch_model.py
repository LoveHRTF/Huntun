#!/usr/bin/env python3
"""Downloads one GGUF set of a Hugging Face repo and records the path llama-server should load.

The repo may hold several sets (the main build plus an experimental MTP variant, a vision projector, ...).
This picks the set to download, shows its size, checks free disk space, downloads it with huggingface_hub,
and writes <model_dir>/model.path (first shard) and, with --vision, <model_dir>/mmproj.path.

Usage: fetch_model.py REPO MODEL_DIR [--set SUBSTRING] [--vision] [--list]
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from pathlib import Path

# huggingface_hub reads these when it is first imported; its 10 s defaults make a slow CDN edge abort a 90 GB download.
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "30")
ATTEMPTS = 5

SHARD_RE = re.compile(r"^(?P<stem>.+)-(?P<idx>\d{5})-of-(?P<total>\d{5})\.gguf$")


def group_sets(files: dict[str, int]) -> dict[str, list[str]]:
    """Groups the repo's .gguf files (path -> size) into model sets: shards of one model, or a single file. mmproj files are left out."""
    sets: dict[str, list[str]] = {}
    for path in sorted(files):
        name = path.rsplit("/", 1)[-1]
        if not name.endswith(".gguf") or "mmproj" in name.lower():
            continue
        m = SHARD_RE.match(path)
        key = m.group("stem") if m else path[: -len(".gguf")]
        sets.setdefault(key, []).append(path)
    return sets


def complete(key: str, paths: list[str]) -> bool:
    m = SHARD_RE.match(paths[0])
    return not m or len(paths) == int(m.group("total"))


def choose(sets: dict[str, list[str]], want: str) -> tuple[str | None, list[str]]:
    """The set to download. Returns (key, candidates); key is None when the choice is ambiguous.

    With `want`: the sets containing it, and among several the one whose name ends with it ("main" picks "-main", not
    "-mainline"). Without: sets that are not MTP variants, and among several the one built for mainline llama.cpp,
    which is what this kit runs (repos often ship a "-mainline" set next to one for a fork or an experimental variant).
    """
    keys = [k for k in sets if complete(k, sets[k])]
    if want:
        w = want.lower()
        keys = [k for k in keys if w in k.lower()]
        exact = [k for k in keys if k.lower().endswith(w)]
        if len(keys) > 1 and len(exact) == 1:
            return exact[0], keys
    else:
        plain = [k for k in keys if "mtp" not in k.lower()]
        keys = plain or keys
        mainline = [k for k in keys if "mainline" in k.lower()]
        if len(keys) > 1 and len(mainline) == 1:
            return mainline[0], keys
    return (keys[0] if len(keys) == 1 else None), keys


def pick_mmproj(files: dict[str, int], set_key: str) -> str | None:
    projs = [p for p in files if p.endswith(".gguf") and "mmproj" in p.rsplit("/", 1)[-1].lower()]
    folder = set_key.rsplit("/", 1)[0] if "/" in set_key else ""
    same_folder = [p for p in projs if (p.rsplit("/", 1)[0] if "/" in p else "") == folder]
    pool = same_folder or projs
    f16 = [p for p in pool if re.search(r"(?<![b])f16", p.rsplit("/", 1)[-1].lower())]
    return (f16 or sorted(pool) or [None])[0]


def gib(n: int) -> str:
    return f"{n / 2**30:.1f} GiB"


def list_files(repo: str) -> dict[str, int]:
    from huggingface_hub import HfApi
    from huggingface_hub.hf_api import RepoFile

    return {e.path: e.size for e in HfApi().list_repo_tree(repo, recursive=True) if isinstance(e, RepoFile)}


class AccessDenied(Exception):
    pass


def download(repo: str, model_dir: Path, paths: list[str], optional: list[str] = ()) -> None:
    """Fetches `paths`, retrying network failures (finished files are skipped and partial ones resumed on each try),
    then `optional` files (model card, license) on a best-effort basis: they never fail the download."""
    from huggingface_hub import hf_hub_download, snapshot_download
    from huggingface_hub.errors import EntryNotFoundError, GatedRepoError, RepositoryNotFoundError, RevisionNotFoundError

    for attempt in range(1, ATTEMPTS + 1):
        try:
            snapshot_download(repo_id=repo, local_dir=str(model_dir), allow_patterns=paths)
            break
        except GatedRepoError as e:
            raise AccessDenied(str(e).splitlines()[0]) from None
        except (RepositoryNotFoundError, RevisionNotFoundError, EntryNotFoundError):
            raise
        except Exception as e:
            if attempt == ATTEMPTS:
                raise
            print(f"\nDownload interrupted ({type(e).__name__}: {e}); retrying ({attempt}/{ATTEMPTS - 1}), "
                  "already downloaded data is kept.", file=sys.stderr)
            time.sleep(10 * attempt)
    for p in optional:
        try:
            hf_hub_download(repo_id=repo, filename=p, local_dir=str(model_dir))
        except Exception as e:
            print(f"Skipped {p} ({type(e).__name__}); read it at https://huggingface.co/{repo}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("model_dir")
    ap.add_argument("--set", default="", help="substring selecting one GGUF set")
    ap.add_argument("--vision", action="store_true", help="also fetch the mmproj vision projector")
    ap.add_argument("--list", action="store_true", help="only list the sets in the repo")
    a = ap.parse_args()

    files = list_files(a.repo)
    sets = group_sets(files)
    if not sets:
        print(f"No .gguf files found in {a.repo}", file=sys.stderr)
        return 1
    print(f"GGUF sets in {a.repo}:")
    for k, paths in sets.items():
        flag = "" if complete(k, paths) else "  (incomplete)"
        print(f"  {k}: {len(paths)} file(s), {gib(sum(files[p] for p in paths))}{flag}")
    if a.list:
        return 0

    key, candidates = choose(sets, a.set)
    if key is None:
        print("\nCould not pick one set automatically. Set MODEL_SET to a substring of one of:", file=sys.stderr)
        for k in candidates or sets:
            print(f"  {k}", file=sys.stderr)
        return 2
    wanted = list(sets[key])
    mmproj = pick_mmproj(files, key) if a.vision else None
    if mmproj:
        wanted.append(mmproj)
    extras = [p for p in files if p.rsplit("/", 1)[-1].lower() in ("readme.md", "license", "license.md")]

    model_dir = Path(a.model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    need = sum(files[p] for p in wanted if not (model_dir / p).exists() or (model_dir / p).stat().st_size != files[p])
    free = shutil.disk_usage(model_dir).free
    print(f"\nSelected: {key} ({len(sets[key])} file(s)){' + ' + mmproj if mmproj else ''}")
    print(f"To download: {gib(need)}; free on {model_dir}: {gib(free)}")
    if need > free - 5 * 2**30:
        print("Not enough disk space (keeping 5 GiB spare).", file=sys.stderr)
        return 3

    try:
        download(a.repo, model_dir, wanted, extras)
    except AccessDenied as e:
        print(f"\n{a.repo} is gated: its author requires you to accept terms before downloading ({e}).", file=sys.stderr)
        print(f"  1. Sign in at https://huggingface.co, open https://huggingface.co/{a.repo} and accept the access terms", file=sys.stderr)
        print("     (if the author approves requests by hand, wait until the page no longer asks you to request access).", file=sys.stderr)
        print("  2. Log in on this machine (setup.ps1 login / setup.sh login), then run download again.", file=sys.stderr)
        return 4

    first = model_dir / sets[key][0]
    (model_dir / "model.path").write_text(str(first) + "\n")
    proj_file = model_dir / "mmproj.path"
    if mmproj:
        proj_file.write_text(str(model_dir / mmproj) + "\n")
    elif proj_file.exists():
        proj_file.unlink()
    print(f"\nModel entry point: {first}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
