"""Prepare IndexTTS auxiliary models in the configured checkpoint directory."""

from __future__ import annotations

import os

from indextts.utils.model_download import ensure_models_available


def main() -> None:
    model_dir = os.environ.get("INDEXTTS_MODEL_DIR", "/data1/ximizhou/indextts/checkpoints")
    print(ensure_models_available(model_dir), flush=True)


if __name__ == "__main__":
    main()
