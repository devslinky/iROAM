import requests
import zipfile
import io
from pathlib import Path
from apps.analytics.gtfs_static import *
from datetime import datetime,date
from datetime import date
import pandas as pd

GTFS_ZIP_URL = "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/b811ead4-6eaf-4adb-8408-d389fb5a069c/resource/c920e221-7a1c-488b-8c5b-6d8cd4e85eaf/download/completegtfs.zip"

def download_ttc_gtfs(output_dir: str = "Complete GTFS") -> None:
    """ downloads newest TTC GTFS Static bundle and extracts it to the specified output directory """

    print("Downloading TTC merged GTFS bundle...")
    resp = requests.get(GTFS_ZIP_URL, timeout=120)
    resp.raise_for_status()

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        print(f"Contents: {zf.namelist()}")
        zf.extractall(out)

    print(f"Extracted to {out}/")
    print(f"Files: {[f.name for f in out.iterdir()]}")

def check_feed_stale(GTFS_DIR: str = "Complete GTFS") -> bool:
    """Checks if the TTC GTFS feed is stale by checking if today falls within the feed window in feed_info.txt."""

    feed_info = pd.read_csv(Path(GTFS_DIR) / "feed_info.txt", dtype=str).iloc[0]

    feed_start = date(int(feed_info["feed_start_date"][:4]),
                        int(feed_info["feed_start_date"][4:6]),
                        int(feed_info["feed_start_date"][6:8]))
    feed_end = date(int(feed_info["feed_end_date"][:4]),
                        int(feed_info["feed_end_date"][4:6]),
                        int(feed_info["feed_end_date"][6:8]))
    today = date.today()
    is_stale = not (feed_start <= today <= feed_end)

    return is_stale


if __name__ == "__main__":
    download_ttc_gtfs("test_gtfs")
