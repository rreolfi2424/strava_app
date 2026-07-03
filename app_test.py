import streamlit as st
import requests
import pandas as pd
import os
from dotenv import load_dotenv

load_dotenv()

@st.cache_data(ttl=3000)  # cache for 50 min, tokens last ~6 hours
def get_access_token():
    response = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": os.getenv("STRAVA_CLIENT_ID"),
            "client_secret": os.getenv("STRAVA_CLIENT_SECRET"),
            "refresh_token": os.getenv("STRAVA_REFRESH_TOKEN"),
            "grant_type": "refresh_token",
        },
    )
    return response.json()["access_token"]

@st.cache_data(ttl=3000)
def get_activities(access_token, per_page=100):
    activities = []
    page = 1
    while True:
        r = requests.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"per_page": per_page, "page": page},
        )
        data = r.json()
        if not data:
            break
        activities.extend(data)
        page += 1
    return pd.DataFrame(activities)

st.title("My Strava Dashboard")

token = get_access_token()
df = get_activities(token)

# Filter to runs and rides
df = df[df["type"].isin(["Run", "Ride"])]
df["start_date"] = pd.to_datetime(df["start_date_local"])
df["distance_km"] = df["distance"] / 1000

st.metric("Total Activities", len(df))
st.metric("Total Distance (km)", round(df["distance_km"].sum(), 1))

col1, col2 = st.columns(2)
with col1:
    activity_type = st.selectbox("Activity Type", ["All"] + df["type"].unique().tolist())

filtered = df if activity_type == "All" else df[df["type"] == activity_type]

st.line_chart(filtered.set_index("start_date")["distance_km"])
st.dataframe(filtered[["name", "type", "start_date", "distance_km", "moving_time"]])