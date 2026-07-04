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


def load_plan(week_start=None):
    sheet = get_sheet()
    records = sheet.get_all_records()
    if not records:
        return pd.DataFrame(columns=["week_start", "day", "type", "planned_distance_mi"])

    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=["week_start", "day", "type", "planned_distance_mi"])

    if "week_start" not in df.columns:
        df["week_start"] = ""
    if "day" not in df.columns:
        df["day"] = ""
    if "type" not in df.columns:
        df["type"] = "Run"
    if "planned_distance_mi" not in df.columns:
        df["planned_distance_mi"] = 0.0

    df["planned_distance_mi"] = pd.to_numeric(df["planned_distance_mi"], errors="coerce").fillna(0)
    df["week_start"] = df["week_start"].fillna("").astype(str)

    if week_start is None:
        return df

    week_key = week_start.strftime("%Y-%m-%d") if hasattr(week_start, "strftime") else str(week_start)
    filtered = df[df["week_start"] == week_key].copy()
    if not filtered.empty:
        return filtered

    legacy = df[df["week_start"] == ""].copy()
    if not legacy.empty:
        legacy["week_start"] = week_key
        return legacy

    return pd.DataFrame(columns=["week_start", "day", "type", "planned_distance_mi"])


def save_plan(df, week_start=None):
    sheet = get_sheet()
    existing = load_plan()

    week_df = pd.DataFrame(columns=["week_start", "day", "type", "planned_distance_mi"])
    if not df.empty:
        week_df = df.copy()
        if "week_start" not in week_df.columns:
            week_df["week_start"] = ""
        if "day" not in week_df.columns:
            week_df["day"] = ""
        if "type" not in week_df.columns:
            week_df["type"] = "Run"
        if "planned_distance_mi" not in week_df.columns:
            week_df["planned_distance_mi"] = 0.0
        week_df["planned_distance_mi"] = pd.to_numeric(week_df["planned_distance_mi"], errors="coerce").fillna(0)
        week_df["week_start"] = week_df["week_start"].fillna("").astype(str)

    if week_start is not None:
        week_key = week_start.strftime("%Y-%m-%d") if hasattr(week_start, "strftime") else str(week_start)
        existing = existing[existing["week_start"] != week_key]
        week_df["week_start"] = week_key
        combined = pd.concat([existing, week_df], ignore_index=True)
    else:
        combined = week_df

    sheet.clear()
    if combined.empty:
        sheet.update([["week_start", "day", "type", "planned_distance_mi"]])
    else:
        sheet.update([combined.columns.values.tolist()] + combined.values.tolist())


# ---------------------------------------------------------------------------
# App layout
# ---------------------------------------------------------------------------


st.set_page_config(page_title="RR Running Tracker", layout="wide")

st.markdown(
    """
    <style>
    .stApp {
        background: linear-gradient(135deg, #140c24 0%, #1d1133 100%);
        color: #f3ebff;
    }
    .stTitle, .stSubheader, .stHeader {
        color: #d8c2ff;
    }
    .stMetric {
        background-color: #24153f;
        border: 1px solid #7c4dff;
        border-radius: 8px;
        padding: 8px;
    }
    div[data-testid="stDataFrame"] {
        border-radius: 8px;
        overflow: hidden;
    }
    .stButton > button, .stDownloadButton > button {
        background-color: #7c4dff;
        color: white;
        border: 1px solid #a78bfa;
    }
    .stTextInput > div > div > input, .stSelectbox > div > div > div, .stNumberInput > div > div > input {
        border: 1px solid #7c4dff;
        background-color: #24153f;
        color: #f3ebff;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

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

plan_totals = []
for week_start in weekly_totals_df["week_start"]:
    week_plan = load_plan(week_start)
    plan_totals.append(round(week_plan["planned_distance_mi"].sum(), 2))

weekly_totals_df["planned_miles"] = plan_totals

st.subheader("Running miles by week (last 3 months)")
st.line_chart(
    weekly_totals_df.set_index("week_start")[["miles", "planned_miles"]],
    color=["#a02196", "#8200ec"],
)

selected_week_label = st.selectbox(
    "Select a week",
    options=weekly_totals_df["week_label"].tolist(),
    index=weekly_totals_df["week_label"].tolist().index(weekly_totals_df.iloc[-1]["week_label"]),
)
selected_week_start = weekly_totals_df.loc[weekly_totals_df["week_label"] == selected_week_label, "week_start"].iloc[0]
selected_week_key = selected_week_start.strftime("%Y-%m-%d")

week_runs = runs[(runs["start_date"] >= selected_week_start) & (runs["start_date"] < selected_week_start + pd.Timedelta(days=7))]
selected_week_total = round(week_runs["distance_mi"].sum(), 2)
st.metric("Selected week total miles", selected_week_total)

plan_df = load_plan(selected_week_start)

rows = []
for offset in range(7):
    day_start = selected_week_start + pd.Timedelta(days=offset)
    day_end = day_start + pd.Timedelta(days=1)
    day_runs = week_runs[(week_runs["start_date"] >= day_start) & (week_runs["start_date"] < day_end)]
    day_name = day_start.strftime("%A")
    planned_day_rows = plan_df[(plan_df["day"] == day_name) & (plan_df["type"] == "Run")]
    planned_miles = round(planned_day_rows["planned_distance_mi"].sum(), 2) if not planned_day_rows.empty else 0.0
    rows.append({
        "Day": day_name,
        "Date": day_start.strftime("%b %d"),
        "Planned (mi)": planned_miles,
        "Actual (mi)": round(day_runs["distance_mi"].sum(), 2),
        "Runs": "; ".join(day_runs["name"].tolist()) if not day_runs.empty else "No runs",
    })

# add planned total next to the actua total metric
planned_week_total = round(plan_df["planned_distance_mi"].sum(), 2)
st.metric("Planned miles for this week", planned_week_total)

week_table = pd.DataFrame(rows)
st.subheader("Runs for the selected week")
st.dataframe(week_table[["Day", "Date", "Planned (mi)", "Actual (mi)", "Runs"]], use_container_width=True, hide_index=True)

st.divider()

st.subheader("Plan for selected week")
# plan_total = round(plan_df["planned_distance_mi"].sum(), 2)
# st.metric("Planned miles for this week", plan_total)

st.caption("Edit the week plan directly below. Changes are saved for the selected week as you work.")

day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

plan_editor_df = pd.DataFrame({
    "day": day_order,
    "type": ["Run"] * 7,
    "planned_distance_mi": [0.0] * 7,
})

if not plan_df.empty:
    for _, row in plan_df.iterrows():
        day_name = row.get("day")
        if day_name in day_order:
            plan_editor_df.loc[plan_editor_df["day"] == day_name, "type"] = row.get("type", "Run")
            plan_editor_df.loc[plan_editor_df["day"] == day_name, "planned_distance_mi"] = row.get("planned_distance_mi", 0.0)

plan_editor_df["day"] = pd.Categorical(plan_editor_df["day"], categories=day_order, ordered=True)
plan_editor_df = plan_editor_df.sort_values("day").reset_index(drop=True)

edited_plan_df = st.data_editor(
    plan_editor_df,
    hide_index=True,
    disabled=["day"],
    use_container_width=True,
    key=f"plan_editor_{selected_week_key}",
)

if edited_plan_df is not None:
    edited_plan_df = edited_plan_df.copy()
    edited_plan_df["week_start"] = selected_week_key
    save_plan(edited_plan_df, week_start=selected_week_start)
