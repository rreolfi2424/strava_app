import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

ACTUAL_COLOR = "#ff6f91"
PLANNED_COLOR = "#7c4dff"
ACTUAL_FILL = "#3a2028"
PLANNED_FILL = "#241b47"
ACTUAL_TEXT = "#ffe8ee"
PLANNED_TEXT = "#efe8ff"

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

# st.write("Client ID:", repr(get_secret("STRAVA_CLIENT_ID")))


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

    df["planned_distance_mi"] = pd.to_numeric(df["planned_distance_mi"], errors="coerce").fillna(0).round(1)
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
        week_df["planned_distance_mi"] = pd.to_numeric(week_df["planned_distance_mi"], errors="coerce").fillna(0).round(1)
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
        values = [combined.columns.astype(str).tolist()]
        for row in combined.itertuples(index=False, name=None):
            values.append([str(item) for item in row])
        sheet.update(values)


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
    .metric-card {
        border-radius: 12px;
        padding: 14px 16px;
        margin-bottom: 8px;
        border: 1px solid rgba(255,255,255,0.12);
        box-shadow: 0 8px 24px rgba(0,0,0,0.18);
    }
    .metric-card.actual {
        background: linear-gradient(135deg, #3a2028 0%, #5a2736 100%);
        border-color: #ff6f91;
    }
    .metric-card.planned {
        background: linear-gradient(135deg, #241b47 0%, #3a2b67 100%);
        border-color: #7c4dff;
    }
    .metric-card .metric-label {
        font-size: 0.8rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: #e4daff;
    }
    .metric-card .metric-value {
        font-size: 1.55rem;
        font-weight: 700;
        margin-top: 4px;
        color: #ffffff;
    }
    .metric-card .metric-subtext {
        font-size: 0.85rem;
        color: #d8c2ff;
        margin-top: 4px;
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
runs["distance_mi"] = round(runs["distance"] / 1000 * 0.621371, 1)

now = pd.Timestamp.now().normalize()
three_months_ago = now - pd.DateOffset(months=3)
runs = runs[runs["start_date"] >= three_months_ago].copy()

if runs.empty:
    st.info("No recent running activities were found in the last 3 months.")
    st.stop()

runs["week_start"] = runs["start_date"].dt.to_period("W-MON").apply(lambda period: period.start_time)

week_starts = pd.date_range(
    start=(three_months_ago - pd.to_timedelta(three_months_ago.weekday(), unit="D")).normalize(),
    end=(now + pd.Timedelta(days=28) - pd.to_timedelta((now + pd.Timedelta(days=28)).weekday(), unit="D")).normalize(),
    freq="W-MON",
)

weekly_totals = []
for week_start in week_starts:
    week_runs = runs[(runs["start_date"] >= week_start) & (runs["start_date"] < week_start + pd.Timedelta(days=7))]
    weekly_totals.append({
        "week_start": week_start,
        "Actual Miles": round(week_runs["distance_mi"].sum(), 1),
    })

weekly_totals_df = pd.DataFrame(weekly_totals).sort_values("week_start")
weekly_totals_df["week_label"] = weekly_totals_df["week_start"].dt.strftime("%b %d, %Y")

plan_totals = []
for week_start in weekly_totals_df["week_start"]:
    week_plan = load_plan(week_start)
    plan_totals.append(round(week_plan["planned_distance_mi"].sum(), 1))

weekly_totals_df["Planned Miles"] = pd.Series(plan_totals).round(1)

st.subheader("Running miles by week (last 3 months)")
st.line_chart(
    weekly_totals_df.set_index("week_start")[["Actual Miles", "Planned Miles"]],
    color=[ACTUAL_COLOR, PLANNED_COLOR],
)

week_labels = weekly_totals_df["week_label"].tolist()
current_week_label = now.strftime("%b %d, %Y")
if current_week_label not in week_labels:
    current_week_label = week_labels[-1]

selected_week_label = st.selectbox(
    "Select a week",
    options=week_labels,
    index=week_labels.index(current_week_label),
)
selected_week_start = weekly_totals_df.loc[weekly_totals_df["week_label"] == selected_week_label, "week_start"].iloc[0]
selected_week_key = selected_week_start.strftime("%Y-%m-%d")

week_runs = runs[(runs["start_date"] >= selected_week_start) & (runs["start_date"] < selected_week_start + pd.Timedelta(days=7))]
selected_week_total = round(week_runs["distance_mi"].sum(), 1)
plan_df = load_plan(selected_week_start)
planned_week_total = round(plan_df["planned_distance_mi"].sum(), 1)
weekly_delta = round(selected_week_total - planned_week_total, 1)
col1, col2 = st.columns(2)
with col1:
    st.markdown(
        f"""
        <div class="metric-card actual">
            <div class="metric-label">Actual miles</div>
            <div class="metric-value">{selected_week_total:.1f}</div>
            <div class="metric-subtext">{weekly_delta:+.1f} vs planned</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
with col2:
    st.markdown(
        f"""
        <div class="metric-card planned">
            <div class="metric-label">Planned miles</div>
            <div class="metric-value">{planned_week_total:.1f}</div>
            <div class="metric-subtext">Target for selected week</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

rows = []
for offset in range(7):
    day_start = selected_week_start + pd.Timedelta(days=offset)
    day_end = day_start + pd.Timedelta(days=1)
    day_runs = week_runs[(week_runs["start_date"] >= day_start) & (week_runs["start_date"] < day_end)]
    day_name = day_start.strftime("%A")
    planned_day_rows = plan_df[(plan_df["day"] == day_name) & (plan_df["type"] == "Run")]
    planned_miles = round(planned_day_rows["planned_distance_mi"].sum(), 1) if not planned_day_rows.empty else 0.0
    rows.append({
        "day": day_name,
        "Date": day_start.strftime("%b %d"),
        "planned_distance_mi": planned_miles,
        "Actual (mi)": round(day_runs["distance_mi"].sum(), 1),
        "Runs": "; ".join(day_runs["name"].tolist()) if not day_runs.empty else "No runs",
    })

week_table = pd.DataFrame(rows)
week_table["planned_distance_mi"] = week_table["planned_distance_mi"].round(1)
week_table["Actual (mi)"] = week_table["Actual (mi)"].round(1)
st.subheader("Planned vs. Actual Miles for Week of " + selected_week_start.strftime("%b %d, %Y"))
display_week_table = week_table[["day", "Date", "planned_distance_mi", "Actual (mi)", "Runs"]].copy()
display_week_table = display_week_table.rename(columns={"day": "Day", "planned_distance_mi": "Planned (mi)"})
display_week_table["Planned (mi)"] = display_week_table["Planned (mi)"].map(lambda value: f"{value:.1f}")
display_week_table["Actual (mi)"] = display_week_table["Actual (mi)"].map(lambda value: f"{value:.1f}")
styled_week_table = (
    display_week_table.style.apply(
        lambda col: [
            f"background-color: {PLANNED_FILL}; color: {PLANNED_TEXT}; font-weight: 600;"
            if col.name == "Planned (mi)"
            else ""
            for _ in col
        ],
        axis=0,
    )
    .apply(
        lambda col: [
            f"background-color: {ACTUAL_FILL}; color: {ACTUAL_TEXT}; font-weight: 600;"
            if col.name == "Actual (mi)"
            else ""
            for _ in col
        ],
        axis=0,
    )
)
st.dataframe(styled_week_table, use_container_width=True, hide_index=True)

st.divider()

st.subheader("Edit Weekly Plan")
# plan_total = round(plan_df["planned_distance_mi"].sum(), 2)
# st.metric("Planned miles for this week", plan_total)

# st.caption("Edit the week plan directly below. Changes are saved for the selected week as you work.")

day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

plan_editor_df = pd.DataFrame({
    "day": day_order,
    "type": ["Run"] * 7,
    "planned_distance_mi": [0.0] * 7,
})

if not plan_df.empty:
    for _, row in plan_df.iterrows():
        day_name = row.get("day", "")
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
