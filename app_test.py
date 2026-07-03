import os

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()


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


st.title("My Strava Dashboard")

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