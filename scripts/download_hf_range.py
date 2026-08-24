#!/usr/bin/env python3
"""Resumable ranged download for large Hugging Face LFS files."""

from __future__ import annotations

import argparse
import hashlib
import os
import time
from pathlib import Path

import requests
from huggingface_hub import hf_hub_url


def download(repo: str, filename: str, target: Path, size: int, sha256: str, *, chunk_size: int, retries: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    if part.exists() and part.stat().st_size > size:
        part.unlink()
    url = hf_hub_url(repo_id=repo, filename=filename, repo_type="model")
    session = requests.Session()
    session.headers.update({"User-Agent": "IndexTTS-LongAudio-Workbench/1.0"})
    offset = part.stat().st_size if part.exists() else 0
    while offset < size:
        end = min(size - 1, offset + chunk_size - 1)
        for attempt in range(1, retries + 1):
            try:
                with session.get(url, headers={"Range": f"bytes={offset}-{end}"}, stream=True, timeout=(30, 300), allow_redirects=True) as response:
                    full_response = response.status_code == 200 and offset == 0 and end == size - 1
                    if response.status_code != 206 and not full_response:
                        raise RuntimeError("server ignored Range header")
                    written = offset
                    with part.open("ab") as stream:
                        for block in response.iter_content(chunk_size=1024 * 1024):
                            if block:
                                if written + len(block) > end + 1:
                                    raise RuntimeError("server returned more data than requested")
                                stream.write(block)
                                written += len(block)
                expected = end + 1
                if written != expected:
                    raise RuntimeError(f"short range: got {written - offset} bytes, expected {expected - offset}")
                offset = written
                print(f"{filename}: {offset}/{size} ({offset / size:.1%})", flush=True)
                break
            except Exception as exc:
                offset = part.stat().st_size if part.exists() else 0
                print(f"{filename}: chunk {offset}-{end} attempt {attempt}/{retries}: {exc}", flush=True)
                if attempt == retries:
                    raise
                time.sleep(min(30, attempt * 3))
    digest = hashlib.sha256()
    with part.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    digest = digest.hexdigest()
    if digest != sha256:
        raise RuntimeError(f"SHA256 mismatch for {filename}: {digest} != {sha256}")
    os.replace(part, target)
    print(f"DONE {target} {size} bytes sha256={digest}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="IndexTeam/IndexTTS-2.5")
    parser.add_argument("--file", required=True)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--size", required=True, type=int)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--chunk-size", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--retries", type=int, default=12)
    args = parser.parse_args()
    download(args.repo, args.file, args.target, args.size, args.sha256, chunk_size=args.chunk_size, retries=args.retries)


if __name__ == "__main__":
    main()
