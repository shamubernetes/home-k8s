"""Wait for real device pairing, then run the unmodified upstream entrypoint."""

import json
import os
from pathlib import Path
import sys
import time


def main():
    os.umask(0o077)
    config_path = Path(os.environ["iSPBTV_data_dir"]) / "config.json"
    if not config_path.exists():
        with config_path.open("x", encoding="utf-8") as handle:
            json.dump({
                "devices": [],
                "skip_categories": ["sponsor"],
                "skip_count_tracking": False,
                "mute_ads": True,
                "skip_ads": True,
                "minimum_skip_length": 1,
                "auto_play": True,
            }, handle, indent=2)
    print("Waiting for YouTube TV pairing. Use upstream --setup-cli to add a device.", flush=True)
    while True:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if config.get("devices"):
                break
        except json.JSONDecodeError:
            # The interactive configurator may still be writing the file.
            pass
        time.sleep(5)
    print("Pairing configuration found; starting iSponsorBlockTV.", flush=True)
    os.execv(sys.executable, [sys.executable, "-u", "/app/main.pyc"])


if __name__ == "__main__":
    main()
