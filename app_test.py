import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

# ---------------------------------------------------------------------------
# Secrets helper (works both locally with .env and on Streamlit Cloud)
# ---------------------------------------------------------------------------

def get_secret(key):
    try:
        return st.secrets[key]
    except (KeyError, FileNotFoundError):
        return os.getenv(key)


# ---------------------------------------------------------------------------
# Strava auth + data fetching
# ---------------------------------------------------------------------------

class StravaApiError(Exception):
    pass

@st.cache_data(ttl=3000)
def get_access_token():
    response = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": os.getenv("STRAVA_CLIENT_ID"),
            "client_secret": os.getenv("STRAVA_CLIENT_SECRET"),
            "refresh_token": os.getenv("STRAVA_REFRESH_TOKEN"),
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    if response.status_code != 200:
        raise StravaApiError(f"Token request failed ({response.status_code}): {response.text}")

    payload = response.json()
    if "access_token" not in payload:
        raise StravaApiError("Token response did not include an access token.")
    return payload["access_token"]


@st.cache_data(ttl=3000)
def get_activities(access_token, per_page=100):
    activities = []
    page = 1
    while True:
        response = requests.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"per_page": per_page, "page": page},
            timeout=20,
        )
        if response.status_code != 200:
            if response.status_code == 429:
                raise StravaApiError("Strava rate limit exceeded. Please wait a moment and try again.")
            raise StravaApiError(f"Activities request failed ({response.status_code}): {response.text}")

        data = response.json()
        if isinstance(data, dict):
            message = data.get("message", "Unexpected Strava response")
            raise StravaApiError(message)
        if not data:
            break
        activities.extend(data)
        page += 1
    return pd.DataFrame(activities)


# ---------------------------------------------------------------------------
# Google Sheets — weekly plan storage
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


@st.cache_resource
def get_sheet():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    client = gspread.authorize(creds)
    sheet = client.open(st.secrets["SHEET_NAME"]).sheet1
    return sheet


def load_plan():
    sheet = get_sheet()
    records = sheet.get_all_records()
    if not records:
        return pd.DataFrame(columns=["day", "type", "planned_distance_mi"])
    df = pd.DataFrame(records)
    if "planned_distance_mi" not in df.columns:
        df["planned_distance_mi"] = 0.0
    df["planned_distance_mi"] = pd.to_numeric(df["planned_distance_mi"], errors="coerce").fillna(0)
    return df


def save_plan(df):
    sheet = get_sheet()
    sheet.clear()
    sheet.update([df.columns.values.tolist()] + df.values.tolist())


# ---------------------------------------------------------------------------
# App layout
# ---------------------------------------------------------------------------


st.set_page_config(page_title="RR Running Tracker", layout="wide")
st.title("RR Running Tracker")

df = pd.DataFrame()

try:
    token = get_access_token()
    df = get_activities(token)
except Exception as exc:
    st.error(f"Unable to load Strava data: {exc}")
    st.stop()

if df.empty:
    st.info("No Strava activities were returned.")
    st.stop()

required_columns = {"type", "start_date_local", "distance", "name"}
missing_columns = required_columns.difference(df.columns)
if missing_columns:
    st.error(f"The Strava response is missing expected columns: {', '.join(sorted(missing_columns))}")
    st.stop()

runs = df[df["type"] == "Run"].copy()
if runs.empty:
    st.info("No running activities were found.")
    st.stop()

runs["start_date"] = pd.to_datetime(runs["start_date_local"]).dt.tz_localize(None)
runs["distance_mi"] = runs["distance"] / 1000 * 0.621371

now = pd.Timestamp.now().normalize()
three_months_ago = now - pd.DateOffset(months=3)
runs = runs[runs["start_date"] >= three_months_ago].copy()

if runs.empty:
    st.info("No recent running activities were found in the last 3 months.")
    st.stop()

runs["week_start"] = runs["start_date"].dt.to_period("W-MON").apply(lambda period: period.start_time)

week_starts = pd.date_range(
    start=(three_months_ago - pd.to_timedelta(three_months_ago.weekday(), unit="D")).normalize(),
    end=(now - pd.to_timedelta(now.weekday(), unit="D")).normalize(),
    freq="W-MON",
)

weekly_totals = []
for week_start in week_starts:
    week_runs = runs[(runs["start_date"] >= week_start) & (runs["start_date"] < week_start + pd.Timedelta(days=7))]
    weekly_totals.append({
        "week_start": week_start,
        "miles": round(week_runs["distance_mi"].sum(), 2),
    })

weekly_totals_df = pd.DataFrame(weekly_totals).sort_values("week_start")
weekly_totals_df["week_label"] = weekly_totals_df["week_start"].dt.strftime("%b %d, %Y")

st.subheader("Running miles by week (last 3 months)")
st.line_chart(weekly_totals_df.set_index("week_start")["miles"])

selected_week_label = st.selectbox(
    "Select a week",
    options=weekly_totals_df["week_label"].tolist(),
    index=weekly_totals_df["week_label"].tolist().index(weekly_totals_df.iloc[-1]["week_label"]),
)
selected_week_start = weekly_totals_df.loc[weekly_totals_df["week_label"] == selected_week_label, "week_start"].iloc[0]

week_runs = runs[(runs["start_date"] >= selected_week_start) & (runs["start_date"] < selected_week_start + pd.Timedelta(days=7))]

selected_week_total = round(week_runs["distance_mi"].sum(), 2)
st.metric("Selected week total miles", selected_week_total)

rows = []
for offset in range(7):
    day_start = selected_week_start + pd.Timedelta(days=offset)
    day_end = day_start + pd.Timedelta(days=1)
    day_runs = week_runs[(week_runs["start_date"] >= day_start) & (week_runs["start_date"] < day_end)]
    rows.append({
        "Day": day_start.strftime("%A"),
        "Date": day_start.strftime("%b %d"),
        "Miles": round(day_runs["distance_mi"].sum(), 2),
        "Runs": "; ".join(day_runs["name"].tolist()) if not day_runs.empty else "No runs",
    })

week_table = pd.DataFrame(rows)
st.subheader("Runs for the selected week")
st.dataframe(week_table[["Day", "Date", "Miles", "Runs"]], use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Weekly plan input
# ---------------------------------------------------------------------------

st.header("This Week's Plan")

plan_df = load_plan()

with st.form("plan_form", clear_on_submit=True):
    fcol1, fcol2, fcol3 = st.columns(3)
    with fcol1:
        day = st.selectbox(
            "Day",
            ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        )
    with fcol2:
        plan_type = st.selectbox("Type", ["Run", "Ride"])
    with fcol3:
        distance = st.number_input("Planned Distance (mi)", min_value=0.0, step=0.5)

    submitted = st.form_submit_button("Add to Plan")
    if submitted:
        new_row = pd.DataFrame(
            [{"day": day, "type": plan_type, "planned_distance_mi": distance}]
        )
        plan_df = pd.concat([plan_df, new_row], ignore_index=True)
        save_plan(plan_df)
        st.success(f"Added {distance}mi {plan_type} on {day}")
        st.rerun()

if not plan_df.empty:
    st.subheader("Current Plan")
    edited = st.data_editor(plan_df, num_rows="dynamic", key="plan_editor")
    if st.button("Save Changes"):
        save_plan(edited)
        st.success("Plan updated")
        st.rerun()

st.divider()

# ---------------------------------------------------------------------------
# Plan vs. Actual comparison
# ---------------------------------------------------------------------------

st.header("Plan vs. Actual — This Week")

today = datetime.now(timezone.utc)
monday = (today - timedelta(days=today.weekday())).replace(
    hour=0, minute=0, second=0, microsecond=0
)
monday = monday.replace(tzinfo=None)

this_week = runs[runs["start_date"] >= monday].copy()
this_week["day"] = this_week["start_date"].dt.day_name()

if not this_week.empty:
    actual_by_day = (
        this_week.groupby(["day", "type"])["distance_mi"]
        .sum()
        .reset_index()
        .rename(columns={"distance_mi": "actual_distance_mi"})
    )
else:
    actual_by_day = pd.DataFrame(columns=["day", "type", "actual_distance_mi"])

if not plan_df.empty:
    comparison = pd.merge(
        plan_df, actual_by_day, on=["day", "type"], how="outer"
    ).fillna(0)
else:
    comparison = actual_by_day.copy()
    comparison["planned_distance_mi"] = 0

day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
comparison["day"] = pd.Categorical(comparison["day"], categories=day_order, ordered=True)
comparison = comparison.sort_values("day")

if comparison.empty:
    st.info("No plan or activity data yet for this week.")
else:
    st.dataframe(comparison)

    chart_data = comparison.groupby("day")[["planned_distance_mi", "actual_distance_mi"]].sum()
    st.bar_chart(chart_data)

    total_planned = comparison["planned_distance_mi"].sum()
    total_actual = comparison["actual_distance_mi"].sum()

    mcol1, mcol2, mcol3 = st.columns(3)
    mcol1.metric("Planned (mi)", round(total_planned, 1))
    mcol2.metric("Actual (mi)", round(total_actual, 1))
    mcol3.metric("Difference", round(total_actual - total_planned, 1))
