"""
app.py
======

Solar Plant Performance Optimization Dashboard.

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from data_processing import (
    DAYTIME_IRRADIATION_THRESHOLD,
    average_performance_ratio,
    build_inverter_dataframe,
    build_master_dataframe,
    daytime_only,
    downtime_event_count,
    filter_master,
    total_ac_energy_kwh,
    total_energy_loss_kwh,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PLANT_COLOURS = {"Plant 1": "#F4A261", "Plant 2": "#2A9D8F"}
HIGHLIGHT_COLOUR = "#E76F51"

st.set_page_config(
    page_title="Solar Plant Performance Optimization Dashboard",
    page_icon="☀️",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Data loaders (cached)
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner="Loading and processing solar plant data...")
def load_master() -> pd.DataFrame:
    return build_master_dataframe()


@st.cache_data(show_spinner=False)
def load_inverter_daily() -> pd.DataFrame:
    return build_inverter_dataframe()


master = load_master()
inverter_daily = load_inverter_daily()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
st.sidebar.title("☀️ Filters")

plant_choice = st.sidebar.radio(
    "Plant",
    options=["Both Plants", "Plant 1", "Plant 2"],
    index=0,
)

min_date = master["DATE_TIME"].min().date()
max_date = master["DATE_TIME"].max().date()

date_range = st.sidebar.slider(
    "Date range",
    min_value=min_date,
    max_value=max_date,
    value=(min_date, max_date),
    format="DD MMM YYYY",
)

time_of_day = st.sidebar.selectbox(
    "Time of day",
    options=["All", "Morning", "Afternoon", "Evening", "Night"],
    index=0,
)

# Source key filter only available when a single plant is selected.
selected_source_keys: list[str] = []
if plant_choice != "Both Plants":
    plant_id = "Plant 1" if plant_choice == "Plant 1" else "Plant 2"
    available_keys = sorted(inverter_daily.loc[inverter_daily["Plant"] == plant_id, "SOURCE_KEY"].unique())
    selected_source_keys = st.sidebar.multiselect(
        "Source keys (inverter heatmap only)",
        options=available_keys,
        default=available_keys,
    )

filtered = filter_master(master, plant_choice, date_range, time_of_day)


# ---------------------------------------------------------------------------
# Header + dynamic insight
# ---------------------------------------------------------------------------
st.title("☀️ Solar Plant Performance Optimization Dashboard")

p1_pr = average_performance_ratio(master[master["Plant"] == "Plant 1"])
p2_pr = average_performance_ratio(master[master["Plant"] == "Plant 2"])
if p1_pr and p2_pr and not np.isnan(p1_pr) and not np.isnan(p2_pr):
    if p1_pr > p2_pr:
        leader, laggard, lead_val, lag_val = "Plant 1", "Plant 2", p1_pr, p2_pr
    else:
        leader, laggard, lead_val, lag_val = "Plant 2", "Plant 1", p2_pr, p1_pr
    pct_lead = ((lead_val - lag_val) / lag_val) * 100
    st.markdown(
        f"**Insight:** {leader} is outperforming {laggard} by "
        f"**{pct_lead:.1f}%** on average performance ratio "
        f"({lead_val * 100:.1f}% vs {lag_val * 100:.1f}%)."
    )

st.caption(
    f"Showing **{len(filtered):,}** records "
    f"between **{date_range[0]:%d %b %Y}** and **{date_range[1]:%d %b %Y}** "
    f"for **{plant_choice}** ({time_of_day.lower()})."
)


# ---------------------------------------------------------------------------
# KPI row
# ---------------------------------------------------------------------------
def _delta(current: float, baseline: float, suffix: str = "", as_pct: bool = False) -> str:
    if baseline == 0 or np.isnan(baseline):
        return ""
    if as_pct:
        diff = (current - baseline) * 100
        return f"{diff:+.2f} pp"
    diff = current - baseline
    pct = (diff / baseline) * 100 if baseline else 0
    return f"{pct:+.1f}% vs full dataset"


baseline_energy = total_ac_energy_kwh(master)
baseline_pr = average_performance_ratio(master)
baseline_loss = total_energy_loss_kwh(master)
baseline_down = downtime_event_count(master)

cur_energy = total_ac_energy_kwh(filtered)
cur_pr = average_performance_ratio(filtered)
cur_loss = total_energy_loss_kwh(filtered)
cur_down = downtime_event_count(filtered)

k1, k2, k3, k4 = st.columns(4)
k1.metric(
    "Total AC Energy (kWh)",
    f"{cur_energy:,.0f}",
    _delta(cur_energy, baseline_energy),
)
k2.metric(
    "Avg Performance Ratio (daytime)",
    f"{(cur_pr * 100):.1f}%" if not np.isnan(cur_pr) else "—",
    _delta(cur_pr, baseline_pr, as_pct=True) if not np.isnan(cur_pr) else "",
)
k3.metric(
    "Total Energy Loss (kWh)",
    f"{cur_loss:,.0f}",
    _delta(cur_loss, baseline_loss),
    delta_color="inverse",
)
k4.metric(
    "Downtime Events",
    f"{cur_down:,}",
    f"{cur_down - baseline_down:+,} vs full dataset",
    delta_color="inverse",
)

st.divider()


# ---------------------------------------------------------------------------
# Chart helpers
# ---------------------------------------------------------------------------
def _empty_chart(message: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        annotations=[dict(text=message, showarrow=False, font=dict(size=14))],
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        height=350,
    )
    return fig


def chart_actual_vs_expected(df: pd.DataFrame) -> go.Figure:
    """Chart 1 — Actual vs Expected power over time, coloured by plant."""
    if df.empty:
        return _empty_chart("No data for the current filters")

    # Plot at 15-min cadence; if too dense, downsample to hourly per plant.
    plot_df = df.copy()
    if len(plot_df) > 4000:
        plot_df = (
            plot_df.set_index("DATE_TIME")
            .groupby("Plant")[["AC_POWER", "Expected_Power"]]
            .resample("1h")
            .mean()
            .reset_index()
        )

    fig = go.Figure()
    for plant, colour in PLANT_COLOURS.items():
        sub = plot_df[plot_df["Plant"] == plant]
        if sub.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=sub["DATE_TIME"], y=sub["AC_POWER"],
                name=f"{plant} — Actual", mode="lines",
                line=dict(color=colour, width=1.6),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=sub["DATE_TIME"], y=sub["Expected_Power"],
                name=f"{plant} — Expected", mode="lines",
                line=dict(color=colour, width=1, dash="dash"), opacity=0.7,
            )
        )
    fig.update_layout(
        height=420,
        title="Actual vs Expected Power",
        xaxis_title="Time", yaxis_title="Power (W)",
        legend=dict(orientation="h", yanchor="bottom", y=-0.25),
        xaxis=dict(
            rangeselector=dict(
                buttons=[
                    dict(count=7, label="1W", step="day", stepmode="backward"),
                    dict(count=14, label="2W", step="day", stepmode="backward"),
                    dict(step="all", label="All"),
                ]
            ),
            rangeslider=dict(visible=False),
        ),
    )
    return fig


def chart_daily_pr_trend(df: pd.DataFrame) -> go.Figure:
    """Chart 2 — Daily mean performance ratio per plant, with PR=0.75 reference."""
    day_df = daytime_only(df)
    if day_df.empty:
        return _empty_chart("No daytime data for the current filters")

    daily = (
        day_df.groupby(["Date", "Plant"], as_index=False)["Performance_Ratio"]
        .mean()
    )
    fig = px.line(
        daily, x="Date", y="Performance_Ratio", color="Plant",
        color_discrete_map=PLANT_COLOURS, markers=True,
    )
    fig.add_hline(
        y=0.75, line_dash="dash", line_color="#264653",
        annotation_text="Industry benchmark (0.75)", annotation_position="top right",
    )
    fig.update_layout(
        height=420, title="Daily Performance Ratio Trend",
        yaxis_title="Performance Ratio", xaxis_title="Date",
        legend=dict(orientation="h", yanchor="bottom", y=-0.25),
    )
    return fig


def chart_energy_loss_area(df: pd.DataFrame) -> go.Figure:
    """Chart 3 — Daily total energy loss (kWh) as a filled area per plant."""
    if df.empty:
        return _empty_chart("No data for the current filters")

    daily_loss = (
        df.assign(loss_kwh=df["Loss"] * 0.25 / 1000.0)
        .groupby(["Date", "Plant"], as_index=False)["loss_kwh"]
        .sum()
    )
    fig = go.Figure()
    for plant, colour in PLANT_COLOURS.items():
        sub = daily_loss[daily_loss["Plant"] == plant].sort_values("Date")
        if sub.empty:
            continue
        threshold = sub["loss_kwh"].quantile(0.75)
        normal = sub[sub["loss_kwh"] <= threshold]
        worst = sub[sub["loss_kwh"] > threshold]

        fig.add_trace(
            go.Scatter(
                x=sub["Date"], y=sub["loss_kwh"], mode="lines", fill="tozeroy",
                name=f"{plant} loss",
                line=dict(color=colour), opacity=0.6,
            )
        )
        fig.add_trace(
            go.Scatter(
                x=worst["Date"], y=worst["loss_kwh"], mode="markers",
                name=f"{plant} top 25% loss days",
                marker=dict(color=HIGHLIGHT_COLOUR, size=10, symbol="diamond"),
            )
        )
    fig.update_layout(
        height=420, title="Energy Loss Over Time",
        yaxis_title="Loss (kWh)", xaxis_title="Date",
        legend=dict(orientation="h", yanchor="bottom", y=-0.3),
    )
    return fig


def chart_downtime_bar(df: pd.DataFrame) -> go.Figure:
    """Chart 4 — Stacked daily downtime event count, with worst-3 days annotated."""
    if df.empty or df["Downtime"].sum() == 0:
        return _empty_chart("No downtime events for the current filters")

    daily_down = (
        df.groupby(["Date", "Plant"], as_index=False)["Downtime"].sum()
    )
    fig = px.bar(
        daily_down, x="Date", y="Downtime", color="Plant",
        color_discrete_map=PLANT_COLOURS,
    )

    totals = daily_down.groupby("Date", as_index=False)["Downtime"].sum()
    worst = totals.nlargest(3, "Downtime")
    for _, row in worst.iterrows():
        fig.add_annotation(
            x=row["Date"], y=row["Downtime"],
            text=f"{int(row['Downtime'])} events",
            showarrow=True, arrowhead=2, yshift=10, font=dict(size=11),
        )

    fig.update_layout(
        height=420, title="Daily Downtime Events (stacked by plant)",
        yaxis_title="Downtime intervals (15-min)", xaxis_title="Date",
        barmode="stack",
        legend=dict(orientation="h", yanchor="bottom", y=-0.25),
    )
    return fig


def chart_irradiation_scatter(df: pd.DataFrame) -> go.Figure:
    """Chart 5 — Irradiation vs AC power scatter with per-plant OLS trendlines."""
    if df.empty:
        return _empty_chart("No data for the current filters")
    fig = px.scatter(
        df, x="IRRADIATION", y="AC_POWER", color="Plant",
        color_discrete_map=PLANT_COLOURS, opacity=0.4,
        trendline="ols",
    )
    fig.update_layout(
        height=420, title="Irradiation vs AC Power",
        xaxis_title="Irradiation (normalised)", yaxis_title="AC Power (W)",
        legend=dict(orientation="h", yanchor="bottom", y=-0.25),
    )
    return fig


def chart_inverter_heatmap(plant: str, source_keys: list[str], date_range_local) -> go.Figure | None:
    """Chart 6 — Per-inverter daily AC_POWER heatmap (single plant only)."""
    sub = inverter_daily[
        (inverter_daily["Plant"] == plant)
        & (inverter_daily["SOURCE_KEY"].isin(source_keys))
        & (inverter_daily["Date"] >= date_range_local[0])
        & (inverter_daily["Date"] <= date_range_local[1])
    ]
    if sub.empty:
        return _empty_chart("No inverter data for the current filters")

    pivot = sub.pivot_table(
        index="SOURCE_KEY", columns="Date", values="AC_POWER", aggfunc="sum"
    ).fillna(0)

    fig = go.Figure(
        data=go.Heatmap(
            z=pivot.values,
            x=[d.isoformat() for d in pivot.columns],
            y=pivot.index.tolist(),
            colorscale="Viridis",
            colorbar=dict(title="AC Power (W·intervals)"),
        )
    )
    fig.update_layout(
        height=520,
        title=f"Inverter Heatmap — {plant}",
        xaxis_title="Date", yaxis_title="Inverter (SOURCE_KEY)",
    )
    return fig


def chart_temperature_efficiency(df: pd.DataFrame) -> go.Figure:
    """Chart 7 — Module temperature vs conversion efficiency (daytime, by time-of-day)."""
    day_df = daytime_only(df).dropna(subset=["Conversion_Efficiency", "MODULE_TEMPERATURE"])
    if day_df.empty:
        return _empty_chart("No daytime data for the current filters")
    fig = px.scatter(
        day_df, x="MODULE_TEMPERATURE", y="Conversion_Efficiency",
        color="Time_of_Day", opacity=0.55,
        category_orders={"Time_of_Day": ["Morning", "Afternoon", "Evening"]},
    )
    fig.update_layout(
        height=420, title="Module Temperature vs Conversion Efficiency",
        xaxis_title="Module temperature (°C)",
        yaxis_title="Conversion efficiency (AC/DC, %)",
        legend=dict(orientation="h", yanchor="bottom", y=-0.25),
    )
    return fig


# ---------------------------------------------------------------------------
# Charts layout
# ---------------------------------------------------------------------------
st.subheader("Power output")
c1, c2 = st.columns(2)
with c1:
    st.plotly_chart(chart_actual_vs_expected(filtered), use_container_width=True)
    st.caption(
        "Compares the live AC output (solid) against the irradiation-implied "
        "expected power (dashed). Persistent gaps reveal underperformance windows; "
        "the range buttons zoom to the last week or two for closer inspection."
    )
with c2:
    st.plotly_chart(chart_daily_pr_trend(filtered), use_container_width=True)
    st.caption(
        "Daily-average Performance Ratio per plant on daytime rows only. "
        "The dashed line marks the 0.75 industry benchmark — sustained dips "
        "below it suggest soiling, shading, or inverter issues."
    )

st.subheader("Losses & downtime")
c3, c4 = st.columns(2)
with c3:
    st.plotly_chart(chart_energy_loss_area(filtered), use_container_width=True)
    st.caption(
        "Daily energy loss (Expected − Actual). Diamond markers flag the top "
        "quartile of loss days per plant — these are the highest-priority days "
        "for an operations team to investigate."
    )
with c4:
    st.plotly_chart(chart_downtime_bar(filtered), use_container_width=True)
    st.caption(
        "Counts 15-minute intervals with zero AC power despite usable irradiation "
        "(>0.05). Stacked bars compare plants on the same day; the three worst "
        "days in the filtered window are annotated."
    )

st.subheader("Performance drivers")
c5, c6 = st.columns(2)
with c5:
    st.plotly_chart(chart_irradiation_scatter(filtered), use_container_width=True)
    st.caption(
        "Irradiation vs AC power, with an OLS fit per plant. A flatter slope "
        "or larger residuals indicate a plant that converts sunlight less "
        "consistently."
    )
with c6:
    st.plotly_chart(chart_temperature_efficiency(filtered), use_container_width=True)
    st.caption(
        "Conversion efficiency (AC/DC) against module temperature. The "
        "expected pattern is a mild downward slope above ~45 °C — a clear "
        "signature of thermal derating."
    )

st.subheader("Inverter-level view")
if plant_choice == "Both Plants":
    st.info("Select **Plant 1** or **Plant 2** in the sidebar to see the per-inverter heatmap.")
else:
    if not selected_source_keys:
        st.warning("Pick at least one source key in the sidebar to render the heatmap.")
    else:
        fig6 = chart_inverter_heatmap(plant_choice, selected_source_keys, date_range)
        st.plotly_chart(fig6, use_container_width=True)
        st.caption(
            "Each row is an inverter; each column is a day. Dim rows that should "
            "be bright are persistent under-producers — usually a maintenance "
            "or string-fault issue."
        )


# ---------------------------------------------------------------------------
# Sidebar download + raw rows expander
# ---------------------------------------------------------------------------
csv_bytes = filtered.to_csv(index=False).encode("utf-8")
st.sidebar.download_button(
    "⬇️ Download filtered data (CSV)",
    data=csv_bytes,
    file_name="filtered_solar_data.csv",
    mime="text/csv",
)

with st.expander("Show last 50 rows of filtered data"):
    st.dataframe(filtered.tail(50), use_container_width=True)
