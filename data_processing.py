"""
data_processing.py
==================

Data loading, aggregation, merging, and feature engineering for the
Solar Plant Performance Optimization Dashboard.

The two generation CSVs ship with different DATE_TIME formats:

    * Plant 1 generation : "DD-MM-YYYY HH:MM"    (dayfirst)
    * Plant 2 generation : "YYYY-MM-DD HH:MM:SS" (ISO)

Both weather files use the ISO format. Generation data is recorded at
the inverter level (many SOURCE_KEYs per timestamp) so it MUST be
aggregated to plant level before joining with the plant-level weather
sensor data — otherwise the merge causes a row explosion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import numpy as np
import pandas as pd


DATA_DIR = Path(__file__).parent / "data"

PLANT1_GEN_FILE = DATA_DIR / "Plant_1_Generation_Data.csv"
PLANT1_WEATHER_FILE = DATA_DIR / "Plant_1_Weather_Sensor_Data.csv"
PLANT2_GEN_FILE = DATA_DIR / "Plant_2_Generation_Data.csv"
PLANT2_WEATHER_FILE = DATA_DIR / "Plant_2_Weather_Sensor_Data.csv"

# System efficiency assumption used to derive an Expected_Power proxy from
# the normalised IRRADIATION reading (0.0 - 1.22).
SYSTEM_EFFICIENCY = 0.15
IRRADIATION_TO_WATTS = 1000.0  # normalised irradiation -> W/m^2 proxy

# Daytime / downtime threshold on normalised irradiation.
DAYTIME_IRRADIATION_THRESHOLD = 0.05


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _read_generation(path: Path, dayfirst: bool) -> pd.DataFrame:
    """Read an inverter-level generation CSV and parse DATE_TIME."""
    df = pd.read_csv(path)
    df["DATE_TIME"] = pd.to_datetime(df["DATE_TIME"], dayfirst=dayfirst)
    return df


def _read_weather(path: Path) -> pd.DataFrame:
    """Read a plant-level weather sensor CSV and parse DATE_TIME (ISO)."""
    df = pd.read_csv(path)
    df["DATE_TIME"] = pd.to_datetime(df["DATE_TIME"])
    return df


def load_raw_data() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Load the four raw CSV files with format-correct DATE_TIME parsing.

    Returns
    -------
    (gen1, weather1, gen2, weather2) : tuple of DataFrames
    """
    gen1 = _read_generation(PLANT1_GEN_FILE, dayfirst=True)
    gen2 = _read_generation(PLANT2_GEN_FILE, dayfirst=False)
    weather1 = _read_weather(PLANT1_WEATHER_FILE)
    weather2 = _read_weather(PLANT2_WEATHER_FILE)
    return gen1, weather1, gen2, weather2


# ---------------------------------------------------------------------------
# Aggregation + merge
# ---------------------------------------------------------------------------
def aggregate_generation(gen_df: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse inverter-level rows to plant level per timestamp.

    DC_POWER and AC_POWER are summed across inverters; DAILY_YIELD is
    averaged. SOURCE_KEY is preserved separately on the raw frame for
    the per-inverter heatmap, so it is dropped here.
    """
    aggregated = (
        gen_df.groupby("DATE_TIME", as_index=False)
        .agg(
            DC_POWER=("DC_POWER", "sum"),
            AC_POWER=("AC_POWER", "sum"),
            DAILY_YIELD=("DAILY_YIELD", "mean"),
        )
    )
    return aggregated


def merge_plant(gen_df: pd.DataFrame, weather_df: pd.DataFrame, plant_label: str) -> pd.DataFrame:
    """
    Aggregate generation to plant-level then left-join with weather.

    Parameters
    ----------
    gen_df : raw inverter-level generation frame
    weather_df : plant-level weather sensor frame
    plant_label : "Plant 1" or "Plant 2"
    """
    agg = aggregate_generation(gen_df)
    weather_cols = ["DATE_TIME", "AMBIENT_TEMPERATURE", "MODULE_TEMPERATURE", "IRRADIATION"]
    merged = agg.merge(weather_df[weather_cols], on="DATE_TIME", how="left")
    merged["Plant"] = plant_label
    return merged


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------
def _classify_time_of_day(hour: int) -> str:
    """Bucket an integer hour into Morning / Afternoon / Evening / Night."""
    if 6 <= hour <= 11:
        return "Morning"
    if 12 <= hour <= 17:
        return "Afternoon"
    if 18 <= hour <= 20:
        return "Evening"
    return "Night"


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add helper and engineered columns required by the dashboard.

    Adds: Hour, Date, Expected_Power, Performance_Ratio,
    Conversion_Efficiency, Loss, Downtime, Time_of_Day.
    """
    out = df.copy()

    # Helper columns
    out["Hour"] = out["DATE_TIME"].dt.hour.astype(int)
    out["Date"] = out["DATE_TIME"].dt.date
    out["Time_of_Day"] = out["Hour"].map(_classify_time_of_day)

    # Expected power: normalised irradiation -> W/m^2 proxy * system efficiency.
    out["Expected_Power"] = out["IRRADIATION"] * IRRADIATION_TO_WATTS * SYSTEM_EFFICIENCY

    # Performance Ratio = AC_POWER / Expected_Power, clipped, NaN at night.
    pr = out["AC_POWER"] / out["Expected_Power"].where(out["Expected_Power"] >= 0.001)
    out["Performance_Ratio"] = pr.clip(lower=0, upper=1.5)

    # Conversion efficiency: AC/DC%, NaN where DC is exactly zero.
    dc_safe = out["DC_POWER"].where(out["DC_POWER"] != 0)
    out["Conversion_Efficiency"] = (out["AC_POWER"] / dc_safe) * 100.0

    # Loss: positive = underperformance, negative = overperformance.
    out["Loss"] = out["Expected_Power"] - out["AC_POWER"]

    # Downtime: zero AC power while there is meaningful sunlight.
    out["Downtime"] = np.where(
        (out["AC_POWER"] == 0) & (out["IRRADIATION"] > DAYTIME_IRRADIATION_THRESHOLD),
        1,
        0,
    ).astype(int)

    return out


# ---------------------------------------------------------------------------
# Top-level builders
# ---------------------------------------------------------------------------
def build_master_dataframe() -> pd.DataFrame:
    """
    Build the canonical plant-level master DataFrame across both plants.

    Steps: load -> aggregate generation -> left-join weather per plant ->
    label plant -> concatenate -> add engineered features.
    """
    gen1, weather1, gen2, weather2 = load_raw_data()

    plant1 = merge_plant(gen1, weather1, "Plant 1")
    plant2 = merge_plant(gen2, weather2, "Plant 2")

    master = pd.concat([plant1, plant2], ignore_index=True)
    master = master.sort_values("DATE_TIME").reset_index(drop=True)
    master = add_features(master)
    return master


def build_inverter_dataframe() -> pd.DataFrame:
    """
    Build a per-inverter daily AC_POWER frame for the heatmap chart.

    Inverter-level data is preserved (no aggregation across SOURCE_KEY)
    and rolled up to one row per (Plant, SOURCE_KEY, Date).
    """
    gen1, _, gen2, _ = load_raw_data()
    gen1 = gen1.assign(Plant="Plant 1")
    gen2 = gen2.assign(Plant="Plant 2")
    inv = pd.concat([gen1, gen2], ignore_index=True)
    inv["Date"] = inv["DATE_TIME"].dt.date
    daily = (
        inv.groupby(["Plant", "SOURCE_KEY", "Date"], as_index=False)["AC_POWER"]
        .sum()
    )
    return daily


# ---------------------------------------------------------------------------
# Filtering helpers used by the Streamlit layer
# ---------------------------------------------------------------------------
def filter_master(
    df: pd.DataFrame,
    plant_choice: str,
    date_range: Tuple[pd.Timestamp, pd.Timestamp],
    time_of_day: str,
) -> pd.DataFrame:
    """
    Apply the sidebar filters to the master frame.

    Parameters
    ----------
    plant_choice : "Both Plants" / "Plant 1" / "Plant 2"
    date_range : (start, end) timestamps inclusive on the date component
    time_of_day : "All" / "Morning" / "Afternoon" / "Evening" / "Night"
    """
    out = df
    if plant_choice != "Both Plants":
        out = out[out["Plant"] == plant_choice]

    start, end = pd.to_datetime(date_range[0]), pd.to_datetime(date_range[1])
    out = out[(out["DATE_TIME"] >= start) & (out["DATE_TIME"] <= end + pd.Timedelta(days=1))]

    if time_of_day != "All":
        out = out[out["Time_of_Day"] == time_of_day]

    return out.reset_index(drop=True)


def daytime_only(df: pd.DataFrame) -> pd.DataFrame:
    """Return the subset of rows where IRRADIATION >= the daytime threshold."""
    return df[df["IRRADIATION"] >= DAYTIME_IRRADIATION_THRESHOLD]


# ---------------------------------------------------------------------------
# KPI helpers
# ---------------------------------------------------------------------------
# Each weather/generation reading is a 15-minute interval, so converting
# instantaneous AC power (W) to energy (kWh) uses a 0.25-hour factor and a
# 1/1000 watt->kilowatt factor.
INTERVAL_HOURS = 0.25
WATTS_TO_KW = 1.0 / 1000.0


def total_ac_energy_kwh(df: pd.DataFrame) -> float:
    """Total AC energy (kWh) given 15-minute interval AC_POWER readings (W)."""
    return float(df["AC_POWER"].sum() * INTERVAL_HOURS * WATTS_TO_KW)


def average_performance_ratio(df: pd.DataFrame) -> float:
    """Mean Performance Ratio, restricted to daytime rows, returned as a fraction."""
    day = daytime_only(df)
    if day.empty:
        return float("nan")
    return float(day["Performance_Ratio"].mean(skipna=True))


def total_energy_loss_kwh(df: pd.DataFrame) -> float:
    """Total energy loss in kWh (Expected - Actual, integrated over intervals)."""
    return float(df["Loss"].sum() * INTERVAL_HOURS * WATTS_TO_KW)


def downtime_event_count(df: pd.DataFrame) -> int:
    """Count of 15-minute intervals flagged as downtime."""
    return int(df["Downtime"].sum())
