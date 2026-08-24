#!/usr/bin/env python3
"""Select the preferred GPU when usable, otherwise the freest fallback."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable


def select_gpu(lines: Iterable[str], *, min_free_mib: int, preferred_gpu_id: int) -> int | None:
    candidates: list[tuple[int, int]] = []
    for line in lines:
        try:
            raw_id, raw_free = line.split(",", 1)
            gpu_id, free_mib = int(raw_id.strip()), int(raw_free.strip())
        except (TypeError, ValueError):
            continue
        if free_mib >= min_free_mib:
            candidates.append((gpu_id, free_mib))
    if any(gpu_id == preferred_gpu_id for gpu_id, _ in candidates):
        return preferred_gpu_id
    return max(candidates, key=lambda item: item[1])[0] if candidates else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-free-mib", type=int, required=True)
    parser.add_argument("--preferred-gpu-id", type=int, required=True)
    args = parser.parse_args()
    selected = select_gpu(sys.stdin, min_free_mib=args.min_free_mib, preferred_gpu_id=args.preferred_gpu_id)
    if selected is None:
        raise SystemExit(1)
    print(selected)


if __name__ == "__main__":
    main()
