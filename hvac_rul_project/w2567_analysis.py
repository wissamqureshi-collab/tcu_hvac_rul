"""
W2567 (Derry Road Milton) - CSV-based RUL analysis
Processes minute-level HVAC data spanning ~3 months (2026-05-12 to present)
Outputs results in sites_data.json format for dashboard compatibility
"""

import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from scipy import stats
import requests
from urllib.parse import urlencode
import os
from pathlib import Path

# Site metadata
SITE_CONFIG = {
    "site_id": "W2567",
    "site_name": "DERRY ROAD MILTON",
    "ip": "10.252.61.101",
    "latitude": 43.541944,
    "longitude": -79.826389,
    "address": "179 Derry Road West, Mississauga, ON L5W 1G3"
}

# Episode extraction thresholds
FAN_THRESHOLD = 95.0  # Minimum fan speed % for episode
MIN_EPISODE_MINUTES = 30.0  # Minimum episode duration
R2_THRESHOLD = 0.25  # Minimum R² to calculate RUL
FAILURE_DT = 10.0  # ΔT threshold for filter failure (°C)
QUERY_DAYS = 90  # Historical window (will use available data)

# Weatherbit API
WEATHERBIT_API_KEY = os.getenv("WEATHERBIT_API_KEY", "8c1ecfc1b6b7468e8451fca1b3159267")


def load_csv_from_github(csv_filename: str) -> pd.DataFrame:
    """Load CSV from GitHub repo (raw content URL)."""
    url = f"https://raw.githubusercontent.com/wissamqureshi-collab/tcu_hvac_rul/main/{csv_filename}"
    try:
        print(f"Fetching CSV from GitHub: {url}")
        df = pd.read_csv(url)
        print(f"✓ Loaded {len(df)} rows")
        return df
    except Exception as e:
        print(f"✗ Failed to load from GitHub: {e}")
        # Try local path as fallback
        local_path = Path(f"/home/aillm/{csv_filename}")
        if local_path.exists():
            print(f"Loading from local: {local_path}")
            return pd.read_csv(local_path)
        raise


def parse_and_clean_data(df: pd.DataFrame) -> pd.DataFrame:
    """Parse timestamps, clean column names, handle missing data."""
    # Rename columns (handle possible whitespace issues)
    df.columns = df.columns.str.strip()

    # Parse timestamp
    df["timestamp"] = pd.to_datetime(df["System Time"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    # Extract needed columns (handle case-insensitive matches)
    col_map = {}
    for col in df.columns:
        col_lower = col.lower().strip()
        if "delta" in col_lower and "t" in col_lower:
            col_map["delta_t"] = col
        elif "fan" in col_lower and ("speed" in col_lower or "speed %" in col_lower):
            col_map["fan_speed"] = col
        elif "free" in col_lower and "cool" in col_lower:
            col_map["free_cool"] = col
        elif "damper" in col_lower and "position" in col_lower:
            col_map["damper_pos"] = col

    # Validate critical columns exist
    required = ["delta_t", "fan_speed", "free_cool"]
    missing = [k for k in required if k not in col_map]
    if missing:
        print(f"⚠ Missing columns: {missing}")
        print(f"Available: {list(df.columns)}")

    # Copy over with standardized names
    for key, orig_col in col_map.items():
        df[key] = df[orig_col]

    # Clean numeric columns
    for col in ["delta_t", "fan_speed"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Handle Free-cool Mode as boolean (values like 1, True, or string "Yes")
    if "free_cool" in df.columns:
        df["free_cool_active"] = (
            (df["free_cool"] == 1) |
            (df["free_cool"] == True) |
            (df["free_cool"].astype(str).str.lower().isin(["yes", "true", "1"]))
        ).astype(int)

    # Forward-fill missing values (minute data is dense, occasional gaps OK)
    for col in ["delta_t", "fan_speed"]:
        if col in df.columns:
            df[col] = df[col].ffill(limit=5)

    print(f"✓ Data cleaned: {len(df)} rows from {df['timestamp'].min()} to {df['timestamp'].max()}")
    return df


def extract_episodes(df: pd.DataFrame, min_fan: float = FAN_THRESHOLD, min_duration_min: float = MIN_EPISODE_MINUTES) -> list:
    """
    Extract freecooling episodes from minute-level data.
    Episode = continuous period where fan ≥ min_fan AND free_cool_active=1
    Groups consecutive True values into episodes.
    """
    episodes = []

    # Find where both conditions are met
    in_episode = (df["fan_speed"] >= min_fan) & (df["free_cool_active"] == 1)

    # Group consecutive True values
    episode_groups = (in_episode != in_episode.shift()).cumsum()
    grouped = df.groupby(episode_groups[in_episode])

    for group_id, group_data in grouped:
        if len(group_data) > 1:
            duration_min = (group_data["timestamp"].iloc[-1] - group_data["timestamp"].iloc[0]).total_seconds() / 60

            # Only include episodes meeting minimum duration
            if duration_min >= min_duration_min:
                max_delta_t = group_data["delta_t"].max()
                avg_fan_speed = group_data["fan_speed"].mean()

                # Calculate adjusted hours (fan_speed²)
                adjusted_hours = (duration_min / 60) * (avg_fan_speed / 100) ** 2

                episodes.append({
                    "start_time": group_data["timestamp"].iloc[0],
                    "end_time": group_data["timestamp"].iloc[-1],
                    "duration_min": duration_min,
                    "max_delta_t": max_delta_t,
                    "avg_fan_speed": avg_fan_speed,
                    "adjusted_hours": adjusted_hours
                })

    print(f"✓ Extracted {len(episodes)} episodes (≥{min_duration_min} min, fan ≥{min_fan}%)")
    return episodes


def detect_filter_change(max_deltas: np.ndarray, episodes: list) -> int:
    """
    Detect filter change as sudden ≥5°C drop in max_ΔT values.
    Returns episode index of change, or -1 if no change detected.
    """
    if len(max_deltas) < 3:
        return -1

    for i in range(1, len(max_deltas)):
        drop = max_deltas[i - 1] - max_deltas[i]
        if drop >= 5.0:
            return i
    return -1


def calculate_rul(episodes: list) -> dict:
    """
    Fit linear regression: max_ΔT vs cumulative adjusted hours.
    Auto-detect filter changes; use post-change data for RUL if detected.
    """
    if len(episodes) < 3:
        return {"success": False, "reason": "Insufficient episodes"}

    cumulative_hours = np.cumsum([ep["adjusted_hours"] for ep in episodes])
    max_deltas = np.array([ep["max_delta_t"] for ep in episodes])

    # Detect filter change
    change_idx = detect_filter_change(max_deltas, episodes)

    if change_idx > 0:
        # Use post-change data for RUL calculation
        post_change_episodes = episodes[change_idx:]
        post_change_hours = np.cumsum([ep["adjusted_hours"] for ep in post_change_episodes])
        post_change_deltas = np.array([ep["max_delta_t"] for ep in post_change_episodes])

        # Fit regression to post-change data
        if len(post_change_deltas) >= 3:
            slope, intercept, r_value, p_value, std_err = stats.linregress(post_change_hours, post_change_deltas)
            r2 = r_value ** 2
            using_post_change = True
            used_episodes = post_change_episodes
            used_hours = post_change_hours
            used_deltas = post_change_deltas
        else:
            # Fall back to all data if post-change segment too small
            slope, intercept, r_value, p_value, std_err = stats.linregress(cumulative_hours, max_deltas)
            r2 = r_value ** 2
            using_post_change = False
            used_episodes = episodes
            used_hours = cumulative_hours
            used_deltas = max_deltas
    else:
        # No filter change detected, use all data
        slope, intercept, r_value, p_value, std_err = stats.linregress(cumulative_hours, max_deltas)
        r2 = r_value ** 2
        using_post_change = False
        used_episodes = episodes
        used_hours = cumulative_hours
        used_deltas = max_deltas
        change_idx = -1

    # Check if regression is valid
    if r2 < R2_THRESHOLD or slope <= 0:
        # Special case: post-change data is too flat (early-stage filter)
        # Fall back to using all data with filter_change notation
        if change_idx >= 0 and using_post_change:
            print(f"⚠ Post-change data too noisy (R²={r2:.3f}), falling back to full history")
            slope, intercept, r_value, p_value, std_err = stats.linregress(cumulative_hours, max_deltas)
            r2 = r_value ** 2
            using_post_change = False
            used_episodes = episodes
            used_hours = cumulative_hours
            used_deltas = max_deltas
            current_hours = cumulative_hours[-1]
            current_delta_t = intercept + slope * current_hours

            if r2 < R2_THRESHOLD or slope <= 0:
                reason = f"Insufficient degradation trend (R²={r2:.3f}, slope={slope:.4f})"
                if change_idx >= 0:
                    reason += f" [filter change at episode {change_idx}, but new filter still flat]"
                return {
                    "success": False,
                    "reason": reason,
                    "slope": slope,
                    "r2": r2,
                    "intercept": intercept,
                    "filter_change_detected": change_idx >= 0,
                    "filter_change_episode_idx": change_idx if change_idx >= 0 else None
                }
        else:
            reason = f"Insufficient degradation trend (R²={r2:.3f}, slope={slope:.4f})"
            if change_idx >= 0 and not using_post_change:
                reason += " [post-change data too sparse]"
            return {
                "success": False,
                "reason": reason,
                "slope": slope,
                "r2": r2,
                "intercept": intercept,
                "filter_change_detected": change_idx >= 0,
                "filter_change_episode_idx": change_idx if change_idx >= 0 else None
            }

    # Calculate RUL
    current_hours = used_hours[-1]
    current_delta_t = intercept + slope * current_hours

    hours_to_failure = (FAILURE_DT - intercept) / slope
    remaining_hours = max(0, hours_to_failure - current_hours)

    # Calculate average adjusted hours per day
    first_episode_time = used_episodes[0]["start_time"]
    last_episode_time = used_episodes[-1]["end_time"]
    total_days = (last_episode_time - first_episode_time).days + 1
    avg_adjusted_hours_per_day = used_hours[-1] / total_days if total_days > 0 else 0.1

    rul_days = remaining_hours / avg_adjusted_hours_per_day if avg_adjusted_hours_per_day > 0 else None

    # Urgency logic
    if rul_days is None or rul_days < 0:
        urgency = "FAILED"
    elif rul_days < 14:
        urgency = "URGENT"
    elif rul_days < 30:
        urgency = "WARNING"
    else:
        urgency = "OK"

    return {
        "success": True,
        "slope": slope,
        "intercept": intercept,
        "r2": r2,
        "current_delta_t": current_delta_t,
        "rul_days": max(0, rul_days) if rul_days is not None else None,
        "urgency": urgency,
        "cumulative_hours": cumulative_hours.tolist(),
        "max_deltas": max_deltas.tolist(),
        "episode_start_times": [ep["start_time"].isoformat() for ep in episodes],
        "total_adjusted_hours": float(cumulative_hours[-1]),
        "avg_adjusted_hours_per_day": avg_adjusted_hours_per_day,
        "episodes_count": len(episodes),
        "query_start_date": episodes[0]["start_time"].strftime("%Y-%m-%d"),
        "query_end_date": episodes[-1]["end_time"].strftime("%Y-%m-%d"),
        "filter_change_detected": change_idx >= 0,
        "filter_change_episode_idx": change_idx if change_idx >= 0 else None
    }


def fetch_weatherbit_air_quality(lat: float, lon: float, start_date: str, end_date: str) -> dict:
    """
    Fetch 90-day average air quality from Weatherbit (PM2.5, PM10 in μg/m³)
    Dates in YYYY-MM-DD format
    """
    try:
        print(f"Fetching air quality for ({lat}, {lon}) from {start_date} to {end_date}...")

        # Convert dates to datetime for chunking
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        pm25_values = []
        pm10_values = []

        # Chunk into 30-day periods to avoid "Request too large"
        current = start
        while current < end:
            chunk_end = min(current + timedelta(days=30), end)
            chunk_start_str = current.strftime("%Y-%m-%d")
            chunk_end_str = chunk_end.strftime("%Y-%m-%d")

            params = {
                "lat": lat,
                "lon": lon,
                "start_date": chunk_start_str,
                "end_date": chunk_end_str,
                "key": WEATHERBIT_API_KEY
            }

            url = f"https://api.weatherbit.io/v2.0/history/hourly?{urlencode(params)}"
            response = requests.get(url, timeout=30)
            response.raise_for_status()

            data = response.json()
            if "data" in data:
                for point in data["data"]:
                    if point.get("pm2_5") is not None:
                        pm25_values.append(float(point["pm2_5"]))
                    if point.get("pm10") is not None:
                        pm10_values.append(float(point["pm10"]))

            current = chunk_end

        if not pm25_values or not pm10_values:
            print("⚠ No air quality data returned")
            return None

        air_quality = {
            "pm25": np.mean(pm25_values),
            "pm10": np.mean(pm10_values),
            "data_points": len(pm25_values)
        }
        print(f"✓ Air quality: PM2.5={air_quality['pm25']:.1f}, PM10={air_quality['pm10']:.1f} μg/m³")
        return air_quality

    except Exception as e:
        print(f"✗ Air quality fetch failed: {e}")
        return None


def analyze_w2567():
    """Main analysis pipeline for W2567."""
    print("\n" + "="*60)
    print(f"W2567 Analysis: {SITE_CONFIG['site_name']}")
    print("="*60)

    # Load CSV
    try:
        df = load_csv_from_github("W2567_months_info.csv")
    except FileNotFoundError:
        print("✗ CSV file not found. Trying alternate name...")
        df = load_csv_from_github("W2567_months_info")

    # Clean data
    df = parse_and_clean_data(df)

    # Extract episodes
    episodes = extract_episodes(df)
    if not episodes:
        print("✗ No valid episodes found")
        return None

    # Calculate RUL
    rul_result = calculate_rul(episodes)

    # Fetch air quality
    air_quality = None
    if rul_result.get("success"):
        air_quality = fetch_weatherbit_air_quality(
            SITE_CONFIG["latitude"],
            SITE_CONFIG["longitude"],
            rul_result["query_start_date"],
            rul_result["query_end_date"]
        )

    # Build output record
    site_record = {
        "site_id": SITE_CONFIG["site_id"],
        "site_name": SITE_CONFIG["site_name"],
        "ip": SITE_CONFIG["ip"],
        "address": SITE_CONFIG["address"],
        "latitude": SITE_CONFIG["latitude"],
        "longitude": SITE_CONFIG["longitude"],
        "success": rul_result.get("success", False),
        "data_source": "csv",  # Mark as CSV-based
        "query_timestamp": datetime.utcnow().isoformat() + "Z",
    }

    # Add data always (for context)
    cumul_hours = np.cumsum([ep["adjusted_hours"] for ep in episodes])
    site_record.update({
        "episodes_count": len(episodes),
        "query_start_date": episodes[0]["start_time"].strftime("%Y-%m-%d") if episodes else None,
        "query_end_date": episodes[-1]["end_time"].strftime("%Y-%m-%d") if episodes else None,
        "max_deltas": rul_result.get("max_deltas", [ep["max_delta_t"] for ep in episodes]),
        "cumulative_adjusted_hours": rul_result.get("cumulative_hours", cumul_hours.tolist()),
        "episode_start_times": rul_result.get("episode_start_times", [ep["start_time"].isoformat() for ep in episodes]),
        "total_adjusted_hours": cumul_hours[-1] if len(cumul_hours) > 0 else None,
        "avg_adjusted_hours_per_day": (cumul_hours[-1] / ((episodes[-1]["end_time"] - episodes[0]["start_time"]).days + 1)) if episodes else None,
        "slope": rul_result.get("slope"),
        "intercept": rul_result.get("intercept"),
        "r2": rul_result.get("r2")
    })

    # Filter change context
    if rul_result.get("filter_change_detected"):
        change_idx = rul_result.get("filter_change_episode_idx", -1)
        max_deltas_list = site_record["max_deltas"]
        if change_idx > 0 and change_idx < len(max_deltas_list):
            site_record["filter_change"] = {
                "detected": True,
                "episode_index": change_idx,
                "pre_change_delta_t": float(max_deltas_list[change_idx - 1]) if change_idx > 0 else None,
                "post_change_delta_t": float(max_deltas_list[change_idx]),
                "pre_change_episodes": change_idx,
                "post_change_episodes": len(episodes) - change_idx
            }
            if change_idx < len(episodes):
                site_record["filter_change"]["change_time"] = episodes[change_idx]["start_time"].isoformat()

    # Add air quality
    site_record["air_quality"] = air_quality

    if rul_result.get("success"):
        site_record.update({
            "current_delta_t": rul_result["current_delta_t"],
            "rul_days": rul_result["rul_days"],
            "urgency": rul_result["urgency"],
        })
    else:
        site_record["analysis_error"] = rul_result.get("reason", "Unknown error")
        # For early-stage filters, provide context
        if rul_result.get("filter_change_detected"):
            site_record["analysis_note"] = "Filter change detected. Post-change data insufficient for RUL yet; monitor over next 1-2 months for degradation trend."

    print("\n✓ Analysis complete")
    return site_record


def merge_with_existing(w2567_record: dict, sites_data_path: str = "/home/aillm/sites_data.json"):
    """Merge W2567 analysis into existing sites_data.json."""
    # Load existing data
    if Path(sites_data_path).exists():
        with open(sites_data_path, "r") as f:
            data = json.load(f)
    else:
        data = {"sites": {}}

    # Update W2567 record
    data["sites"]["W2567"] = w2567_record

    # Update metadata
    data["query_timestamp"] = datetime.utcnow().isoformat() + "Z"
    data["sites_with_w2567"] = len(data["sites"])

    # Write back
    with open(sites_data_path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"✓ Merged into {sites_data_path}")
    print(f"  Total sites: {len(data['sites'])}")


if __name__ == "__main__":
    w2567_record = analyze_w2567()
    if w2567_record:
        print("\n" + "="*60)
        print("Output Record:")
        print("="*60)
        print(json.dumps(w2567_record, indent=2, default=str))

        # Merge into sites_data.json
        print("\nMerging with existing sites_data.json...")
        merge_with_existing(w2567_record)
