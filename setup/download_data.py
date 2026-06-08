"""
Fetches raw datasets for the ARCTIC SciSynth 2026 RWD track.

Usage:
    conda run -n arctic-scisynth-2026-rwd python setup/download_data.py
    conda run -n arctic-scisynth-2026-rwd python setup/download_data.py --force

Datasets:
    1. MARTA GTFS feed  (itsmarta.com)
    2. FTA NTD monthly ridership  (transit.dot.gov)
    3. Census ACS B08301 + B08141  (api.census.gov)
    4. World Cup reference CSV  (hand-built, written to data/processed/)

All raw files land in data/raw/. The World Cup reference is written
directly to data/processed/ because it's hand-built (no upstream source)
— this is the one file that bypasses the "notebook generates processed
files" pattern in the SPEC.

Each fetch is idempotent: skips if the target file already exists unless
--force is passed.

IMPORTANT: If a dataset URL, schema, or format has changed from what this
script expects, it prints a clear warning and does NOT silently work
around the change. See the Phase 0 plan's "surface dataset surprises"
constraint.
"""

import argparse
import csv
import io
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"

# ---------------------------------------------------------------------------
# HTTP session — some government sites (transit.dot.gov) reject Python's
# default User-Agent. All requests go through this session.
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def ensure_dirs():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)


def should_skip(path: Path, force: bool) -> bool:
    """Return True (and print a skip message) if the file exists and --force
    was not passed."""
    if path.exists() and not force:
        print(f"  \u26a0\ufe0f  skipped (cached): {path.relative_to(ROOT)}")
        return True
    return False


# ---------------------------------------------------------------------------
# 1. MARTA GTFS feed
# ---------------------------------------------------------------------------

# Correct URL includes the /google_transit_feed/ directory.
# The root path (itsmarta.com/google_transit.zip) returns the trip planner
# HTML page, not the feed.
GTFS_PRIMARY_URL = "https://www.itsmarta.com/google_transit_feed/google_transit.zip"
GTFS_FALLBACK_PAGE = "https://itsmarta.com/app-developer-resources.aspx"
GTFS_TARGET = RAW_DIR / "marta_gtfs.zip"

# Date the NextGen Bus Network is expected to appear in GTFS feeds.
NEXTGEN_CUTOVER = date(2026, 4, 18)


def fetch_marta_gtfs(force: bool = False):
    """Download the MARTA GTFS feed and check its effective date."""
    print("\n[1/4] MARTA GTFS feed")

    if should_skip(GTFS_TARGET, force):
        return

    print(f"  Fetching {GTFS_PRIMARY_URL} ...")
    resp = SESSION.get(GTFS_PRIMARY_URL, timeout=60)

    if resp.status_code != 200:
        # ---- DATASET SURPRISE: primary URL failed ----
        print(f"  \u26a0\ufe0f  Primary URL returned HTTP {resp.status_code}.")
        print(f"  \u26a0\ufe0f  DATASET SURPRISE: The GTFS download URL may have changed.")
        print(f"  \u26a0\ufe0f  Check {GTFS_FALLBACK_PAGE} manually and update GTFS_PRIMARY_URL.")
        print(f"  \u26a0\ufe0f  Attempting to scrape fallback page ...")

        page = SESSION.get(GTFS_FALLBACK_PAGE, timeout=30)
        if page.status_code != 200:
            print(f"  \u274c  Fallback page also failed (HTTP {page.status_code}).")
            print(f"  \u274c  Please download the GTFS feed manually from:")
            print(f"       {GTFS_FALLBACK_PAGE}")
            print(f"       Save it as: {GTFS_TARGET}")
            sys.exit(1)

        # Crude scrape: look for .zip links
        zip_links = re.findall(r'href="([^"]*google_transit[^"]*\.zip)"', page.text, re.I)
        if not zip_links:
            zip_links = re.findall(r'href="([^"]*gtfs[^"]*\.zip)"', page.text, re.I)

        if not zip_links:
            print(f"  \u274c  Could not find a GTFS .zip link on the fallback page.")
            print(f"  \u274c  Please download manually from: {GTFS_FALLBACK_PAGE}")
            sys.exit(1)

        fallback_url = zip_links[0]
        if not fallback_url.startswith("http"):
            fallback_url = "https://itsmarta.com/" + fallback_url.lstrip("/")

        print(f"  \u26a0\ufe0f  DATASET SURPRISE: Using fallback URL: {fallback_url}")
        resp = SESSION.get(fallback_url, timeout=60)
        if resp.status_code != 200:
            print(f"  \u274c  Fallback URL also failed (HTTP {resp.status_code}).")
            sys.exit(1)

    # Verify it's actually a ZIP
    if not resp.content[:4] == b"PK\x03\x04":
        print(f"  \u274c  Downloaded file is not a valid ZIP archive.")
        print(f"  \u274c  DATASET SURPRISE: MARTA may have changed their GTFS distribution format.")
        sys.exit(1)

    GTFS_TARGET.write_bytes(resp.content)
    size_mb = len(resp.content) / (1024 * 1024)
    print(f"  \u2705  fetched: {GTFS_TARGET.relative_to(ROOT)} ({size_mb:.1f} MB)")

    # Check feed effective date
    _check_gtfs_feed_date()


def _check_gtfs_feed_date():
    """Read feed_info.txt from the GTFS ZIP and check if it's pre-NextGen.

    If feed_info.txt is absent, fall back to calendar.txt for service
    date range.
    """
    try:
        with zipfile.ZipFile(GTFS_TARGET) as zf:
            names = zf.namelist()
            print(f"  Files in ZIP: {', '.join(sorted(names)[:10])}"
                  f"{'...' if len(names) > 10 else ''}")

            if "feed_info.txt" in names:
                with zf.open("feed_info.txt") as f:
                    reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
                    for row in reader:
                        start = row.get("feed_start_date", "unknown")
                        end = row.get("feed_end_date", "unknown")
                        print(f"  Feed effective: {start} to {end}")
                        _compare_to_nextgen(start)
                        break  # Only one row expected
            elif "calendar.txt" in names:
                # Fallback: use calendar.txt start_date/end_date range
                print(f"  \u26a0\ufe0f  No feed_info.txt found. "
                      f"Checking calendar.txt for service dates ...")
                with zf.open("calendar.txt") as f:
                    reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
                    starts, ends = [], []
                    for row in reader:
                        starts.append(row.get("start_date", ""))
                        ends.append(row.get("end_date", ""))
                    if starts:
                        earliest = min(s for s in starts if s)
                        latest = max(e for e in ends if e)
                        print(f"  Service date range: {earliest} to {latest}")
                        _compare_to_nextgen(earliest)
                    else:
                        print(f"  \u26a0\ufe0f  calendar.txt is empty or unparseable.")
            else:
                print(f"  \u26a0\ufe0f  No feed_info.txt or calendar.txt found. "
                      f"Cannot verify feed effective date.")
    except zipfile.BadZipFile:
        print(f"  \u274c  GTFS file is not a valid ZIP. Re-download may be needed.")


def _compare_to_nextgen(start_str: str):
    """Compare a YYYYMMDD date string to the NextGen cutover date."""
    try:
        start_date = date(int(start_str[:4]), int(start_str[4:6]),
                          int(start_str[6:8]))
        if start_date < NEXTGEN_CUTOVER:
            print(f"  \u26a0\ufe0f  Feed start date ({start_date}) is BEFORE "
                  f"the NextGen Bus Network cutover ({NEXTGEN_CUTOVER}).")
            print(f"  \u26a0\ufe0f  Rail station data is unaffected; bus routes "
                  f"are pre-redesign.")
            print(f"  \u26a0\ufe0f  This is expected if running before April 18, "
                  f"2026. Proceeding.")
        else:
            print(f"  \u2705  Feed reflects post-NextGen service "
                  f"(start {start_date} >= {NEXTGEN_CUTOVER}).")
    except (ValueError, IndexError):
        print(f"  \u26a0\ufe0f  Could not parse date: {start_str!r}")


# ---------------------------------------------------------------------------
# 2. FTA NTD monthly ridership
# ---------------------------------------------------------------------------

NTD_LANDING = (
    "https://www.transit.dot.gov/ntd/data-product/"
    "monthly-module-adjusted-data-release"
)
NTD_TARGET = RAW_DIR / "ntd_monthly_ridership.xlsx"


def fetch_ntd_ridership(force: bool = False):
    """Scrape the NTD landing page for the latest .xlsx link and download it.

    MANUAL DOWNLOAD REQUIRED: transit.dot.gov blocks automated requests
    (CloudFlare WAF returns 403 regardless of User-Agent). If the
    automated fetch fails, the script prints instructions for manual
    download. This is a known issue — the pre-camp checklist and
    setup/README.md should note that this step requires a browser.

    To download manually:
      1. Go to: https://www.transit.dot.gov/ntd/data-product/monthly-module-adjusted-data-release
      2. Download the latest "Complete Monthly Ridership" .xlsx (~14 MB)
      3. Save as: data/raw/ntd_monthly_ridership.xlsx

    NTD preamble-rows gotcha: The FTA Excel data sheets have preamble
    rows (agency info, notes) ABOVE the column header row. The correct
    skiprows= value must be determined empirically during Phase 0
    execution and baked into the pipeline notebook with an explicit
    comment. FTA reformats this file occasionally across years, so the
    magic number is NOT stable. Do not silently patch.
    """
    print("\n[2/4] FTA NTD monthly ridership")

    if should_skip(NTD_TARGET, force):
        return

    print(f"  Fetching landing page: {NTD_LANDING} ...")
    resp = SESSION.get(NTD_LANDING, timeout=30)
    if resp.status_code != 200:
        print(f"  \u274c  Landing page returned HTTP {resp.status_code}.")
        print(f"  \u274c  DATASET SURPRISE: NTD page may have been restructured.")
        print(f"  \u274c  Please download the latest .xlsx manually from:")
        print(f"       {NTD_LANDING}")
        print(f"       Save it as: {NTD_TARGET}")
        # Don't abort — NTD is a documented manual step; let Census and
        # World Cup still run. main() checks NTD_TARGET at the end.
        return

    # Parse out .xlsx links
    xlsx_links = re.findall(
        r'href="([^"]*\.xlsx[^"]*)"', resp.text, re.I
    )

    if not xlsx_links:
        print(f"  \u274c  No .xlsx links found on the NTD landing page.")
        print(f"  \u274c  DATASET SURPRISE: FTA may have changed the file format or page layout.")
        print(f"  \u274c  Please download manually from: {NTD_LANDING}")
        print(f"       Save it as: {NTD_TARGET}")
        return

    xlsx_url = xlsx_links[0]  # most recent release is typically first
    if not xlsx_url.startswith("http"):
        xlsx_url = "https://www.transit.dot.gov" + xlsx_url

    print(f"  Found .xlsx link: {xlsx_url}")
    print(f"  Downloading (~14 MB, may take a minute) ...")
    dl = SESSION.get(xlsx_url, timeout=120)
    if dl.status_code != 200:
        print(f"  \u274c  Download failed (HTTP {dl.status_code}).")
        print(f"  \u274c  Response body: {dl.text[:200]}")
        print(f"  \u274c  Please download manually from: {xlsx_url}")
        return

    NTD_TARGET.write_bytes(dl.content)
    size_mb = len(dl.content) / (1024 * 1024)
    print(f"  \u2705  fetched: {NTD_TARGET.relative_to(ROOT)} ({size_mb:.1f} MB)")


# ---------------------------------------------------------------------------
# 3. Census ACS B08301 + B08141
# ---------------------------------------------------------------------------

# Variable codes (verified against the live Census API):
#
# B08301 — Means of Transportation to Work (universe: workers 16+)
#   B08301_001E — total workers (denominator)
#   B08301_003E — drove alone
#   B08301_010E — public transportation (excluding taxicab)
#
# B08141 — Means of Transportation to Work by Vehicles Available
#   (universe: workers 16+, NOT households — see plan for nuance)
#   B08141_001E — total workers (denominator)
#   B08141_002E — workers in zero-vehicle households
#
# Geography: all tracts in Fulton (FIPS 121) and DeKalb (FIPS 089)
# counties, Georgia (FIPS state 13).

CENSUS_BASE = "https://api.census.gov/data"

# Try 2024 5-year first (2020–2024 release, expected Jan 29 2026);
# fall back to 2023 5-year (2019–2023 release).
CENSUS_YEARS = [2024, 2023]

CENSUS_TABLES = {
    "b08301": {
        "variables": "NAME,B08301_001E,B08301_003E,B08301_010E",
        "target": RAW_DIR / "acs_b08301_fulton_dekalb.csv",
    },
    "b08141": {
        "variables": "NAME,B08141_001E,B08141_002E",
        "target": RAW_DIR / "acs_b08141_fulton_dekalb.csv",
    },
}


def fetch_census_commute(force: bool = False):
    """Fetch ACS commute data via direct requests to api.census.gov (Plan A).

    If direct API fails, prints actionable instructions for Plan B
    (census package) or Plan C (cenpy) but does NOT install them
    automatically.
    """
    print("\n[3/4] Census ACS commute data")

    # Check if all tables are already cached (without side-effecting prints)
    if not force and all(t["target"].exists() for t in CENSUS_TABLES.values()):
        for t in CENSUS_TABLES.values():
            print(f"  \u26a0\ufe0f  skipped (cached): {t['target'].relative_to(ROOT)}")
        return

    vintage = _find_census_vintage()
    if vintage is None:
        print(f"  \u274c  Could not find a working ACS 5-year endpoint.")
        print(f"  \u274c  DATASET SURPRISE: Census API may be down or the endpoint changed.")
        print(f"  \u274c  Plan B fallback: pip install census (datamade/census on PyPI)")
        print(f"  \u274c  Plan C fallback: pip install cenpy")
        sys.exit(1)

    for table_name, info in CENSUS_TABLES.items():
        if should_skip(info["target"], force):
            continue

        url = (
            f"{CENSUS_BASE}/{vintage}/acs/acs5"
            f"?get={info['variables']}"
            f"&for=tract:*"
            f"&in=state:13+county:121,089"
        )
        print(f"  Fetching {table_name.upper()} from {vintage} ACS 5-year ...")
        resp = SESSION.get(url, timeout=30)

        if resp.status_code != 200:
            print(f"  \u274c  API returned HTTP {resp.status_code} for {table_name}.")
            print(f"  \u274c  Response body: {resp.text[:200]}")
            print(f"  \u274c  DATASET SURPRISE: Variable codes may have changed.")
            print(f"  \u274c  URL attempted: {url}")
            sys.exit(1)

        data = resp.json()
        if not data or len(data) < 2:
            print(f"  \u274c  API returned empty or malformed data for {table_name}.")
            sys.exit(1)

        # Write as CSV: first row is headers, rest is data
        with open(info["target"], "w", newline="") as f:
            writer = csv.writer(f)
            for row in data:
                writer.writerow(row)

        n_tracts = len(data) - 1  # minus header row
        print(f"  \u2705  fetched: {info['target'].relative_to(ROOT)} "
              f"({n_tracts} tracts, vintage {vintage})")


def _find_census_vintage() -> int | None:
    """Try each candidate year and return the first that responds."""
    for year in CENSUS_YEARS:
        url = f"{CENSUS_BASE}/{year}/acs/acs5?get=NAME&for=state:13"
        try:
            resp = SESSION.get(url, timeout=15)
            if resp.status_code == 200:
                print(f"  Using ACS 5-year vintage: {year}")
                return year
            else:
                print(f"  Vintage {year} returned HTTP {resp.status_code}, trying next ...")
        except requests.RequestException as e:
            print(f"  Vintage {year} request failed ({e}), trying next ...")
    return None


# ---------------------------------------------------------------------------
# 4. World Cup reference CSV (hand-built)
# ---------------------------------------------------------------------------

WORLDCUP_TARGET = PROCESSED_DIR / "worldcup_reference.csv"

# 8 Atlanta matches at Mercedes-Benz Stadium, June 15 – July 15, 2026.
# Sources: atlantafwc26.com, Mercedes-Benz Stadium official press release,
# FIFA 2026 draw and schedule.
#
# "Cabo Verde" is the FIFA-official spelling (not "Cape Verde").
#
# NOTE: If any match dates, teams, or rounds change before camp (June 8–12),
# update ATLANTA_MATCHES, bump LAST_VERIFIED, and re-run the script with
# --force. The camp starts 3 days before the first match, so last-minute
# FIFA changes are possible.

# Last time this block was verified against the authoritative sources.
# Update whenever ATLANTA_MATCHES or STADIUM_CAPACITY changes.
LAST_VERIFIED = "2026-04-16"

# Mercedes-Benz Stadium World Cup reconfiguration capacity.
# Sources: FourFourTwo venue guide; worldcupmercedesbenzstadium.com;
# StadiumDB (all April 2026). The 71,000 MLS/Falcons seated figure is
# NOT the tournament configuration.
STADIUM_CAPACITY = 75000

ATLANTA_MATCHES = [
    # (match_id, date, kickoff_local_ET, round, teams)
    # match_id is arbitrary but stable across reruns
    ("ATL-01", "2026-06-15", "12:00", "Group H", "Spain vs Cabo Verde"),
    ("ATL-02", "2026-06-18", "12:00", "Group A", "Czechia/Playoff winner vs South Africa"),
    ("ATL-03", "2026-06-21", "12:00", "Group H", "Spain vs Saudi Arabia"),
    ("ATL-04", "2026-06-24", "18:00", "Group C", "Morocco vs Haiti"),
    ("ATL-05", "2026-06-27", "19:30", "Group Stage", "DR Congo vs Uzbekistan"),
    ("ATL-06", "2026-07-01", "12:00", "Round of 32", "TBD vs TBD"),
    ("ATL-07", "2026-07-07", "12:00", "Round of 16", "TBD vs TBD"),
    ("ATL-08", "2026-07-15", "15:00", "Semifinal", "TBD vs TBD"),
]

# Mercedes-Benz Stadium coordinates (used for match rows below)
MBS_LAT, MBS_LON = 33.7553, -84.4006

LOCATIONS = [
    # (name, lat, lon, type)
    ("Mercedes-Benz Stadium", MBS_LAT, MBS_LON, "stadium"),
    ("Centennial Olympic Park (Fan Fest)", 33.7607, -84.3931, "fan_fest"),
    # Representative centroids for major hotel corridors
    ("Downtown Atlanta", 33.7537, -84.3901, "hotel_cluster"),
    ("Midtown Atlanta", 33.7845, -84.3832, "hotel_cluster"),
    ("Buckhead", 33.8384, -84.3793, "hotel_cluster"),
]


def build_worldcup_reference(force: bool = False):
    """Write the hand-built World Cup reference CSV to data/processed/.

    This is NOT fetched from an upstream source — it's instructor-prepared
    reference data. It goes directly to data/processed/ because it IS the
    source of truth.
    """
    print("\n[4/4] World Cup reference CSV")

    if should_skip(WORLDCUP_TARGET, force):
        return

    rows = []

    for match_id, dt, kickoff, rnd, teams in ATLANTA_MATCHES:
        rows.append({
            "type": "match",
            "name": teams,
            "match_id": match_id,
            "date": dt,
            "kickoff_local": kickoff,
            "round": rnd,
            "lat": MBS_LAT,
            "lon": MBS_LON,
        })

    for name, lat, lon, loc_type in LOCATIONS:
        rows.append({
            "type": loc_type,
            "name": name,
            "match_id": "",
            "date": "",
            "kickoff_local": "",
            "round": "",
            "lat": lat,
            "lon": lon,
        })

    fieldnames = ["type", "name", "match_id", "date", "kickoff_local",
                  "round", "lat", "lon"]
    with open(WORLDCUP_TARGET, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    n_venues = sum(1 for _, _, _, t in LOCATIONS if t in ("stadium", "fan_fest"))
    n_hotels = sum(1 for _, _, _, t in LOCATIONS if t == "hotel_cluster")
    print(f"  \u2705  built: {WORLDCUP_TARGET.relative_to(ROOT)} "
          f"({len(ATLANTA_MATCHES)} matches, "
          f"{n_venues} venues, {n_hotels} hotel clusters)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch raw datasets for ARCTIC SciSynth 2026 RWD track."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-download even if files already exist."
    )
    args = parser.parse_args()

    print("=" * 60)
    print("ARCTIC SciSynth 2026 RWD \u2014 Data Download")
    print("=" * 60)

    ensure_dirs()
    fetch_marta_gtfs(force=args.force)
    fetch_ntd_ridership(force=args.force)
    fetch_census_commute(force=args.force)
    build_worldcup_reference(force=args.force)

    print("\n" + "=" * 60)
    if not NTD_TARGET.exists():
        print("\u26a0\ufe0f  NTD ridership file still missing \u2014 manual download required.")
        print(f"       Download from: {NTD_LANDING}")
        print(f"       Save as:       {NTD_TARGET}")
        print("=" * 60)
        sys.exit(1)
    print("Done. Check output above for any warnings (\u26a0\ufe0f) or errors (\u274c).")
    print("=" * 60)


if __name__ == "__main__":
    main()
