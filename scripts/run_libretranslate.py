"""Keep the local engine's models and caches inside the project."""

import logging
import os
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    runtime = root / "data" / "libretranslate"
    for variable, folder in {
        "XDG_DATA_HOME": "share",
        "XDG_CONFIG_HOME": "config",
        "XDG_CACHE_HOME": "cache",
        "HF_HOME": "huggingface",
        "TORCH_HOME": "torch",
        "STANZA_RESOURCES_DIR": "stanza",
    }.items():
        path = runtime / folder
        path.mkdir(parents=True, exist_ok=True)
        os.environ[variable] = str(path)
    os.environ["ARGOS_PACKAGES_DIR"] = str(runtime / "share" / "argos-translate" / "packages")
    os.environ.setdefault("ARGOS_DEVICE_TYPE", "cpu")
    os.environ.setdefault("ARGOS_CHUNK_TYPE", "MINISBD")
    os.environ["ARGOS_DEBUG"] = "0"
    # Avoid writing the service's API key database into the source tree.
    os.environ["LT_API_KEYS_DB_PATH"] = str(runtime / "api_keys.db")
    # MiniSBD explicitly supports overriding this path at runtime.
    import minisbd.models

    minisbd.models.cache_dir = str(runtime / "cache" / "minisbd")
    from libretranslate.main import main as run

    # Argos logs source text and tokens at INFO by default.
    logging.getLogger("argostranslate.utils").setLevel(logging.WARNING)
    run()


if __name__ == "__main__":
    main()
