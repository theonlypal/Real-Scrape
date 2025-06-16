import json
import os
import sqlite3
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import pandas as pd
import requests
import streamlit as st
from geopy.geocoders import Nominatim
import overpy

INCOME_CSV = os.path.join('data', 'zip_income_sample.csv')
CACHE_DB = 'lead_cache.db'

VERTICAL_TAGS = {
    'Plumbing': {'craft': 'plumber'},
    'Cafe': {'amenity': 'cafe'},
    'Pet Grooming': {'shop': 'pet'},
    'Medical Clinic': {'amenity': 'clinic'},
    'Specialty Retail': {'shop': 'electronics'}
}


def slugify(value: str) -> str:
    return ''.join(c if c.isalnum() else '-' for c in value.lower()).strip('-')


def init_db():
    conn = sqlite3.connect(CACHE_DB)
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS leads (
            osm_id TEXT PRIMARY KEY,
            data TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS calls (
            osm_id TEXT,
            outcome TEXT,
            timestamp TEXT
        )"""
    )
    conn.commit()
    conn.close()


def save_lead(osm_id: str, data: dict):
    conn = sqlite3.connect(CACHE_DB)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO leads(osm_id, data) VALUES(?, ?)", (osm_id, json.dumps(data)))
    conn.commit()
    conn.close()


def mark_call(osm_id: str, outcome: str):
    conn = sqlite3.connect(CACHE_DB)
    c = conn.cursor()
    c.execute("INSERT INTO calls(osm_id, outcome, timestamp) VALUES(?, ?, ?)", (osm_id, outcome, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def load_called_ids() -> set:
    conn = sqlite3.connect(CACHE_DB)
    c = conn.cursor()
    c.execute("SELECT DISTINCT osm_id FROM calls")
    ids = {row[0] for row in c.fetchall()}
    conn.close()
    return ids


def load_income_data() -> pd.DataFrame:
    df = pd.read_csv(INCOME_CSV)
    df['zip'] = df['zip'].astype(str).str.zfill(5)
    return df


@st.cache_data(show_spinner=False)
def geocode_place(place: str):
    geolocator = Nominatim(user_agent="lead_app")
    location = geolocator.geocode(place)
    if not location:
        st.error("Location not found")
        st.stop()
    return location.latitude, location.longitude


@st.cache_data(show_spinner=False)
def overpass_query(lat: float, lon: float, radius: int, verticals: list, days: int):
    api = overpy.Overpass()
    date_limit = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    queries = []
    for name in verticals:
        tags = VERTICAL_TAGS[name]
        filters = ''.join([f'[{k}="{v}"]' for k, v in tags.items()])
        q = f"node{filters}(around:{radius * 1609},{lat},{lon})[opening_date>\"{date_limit}\"];out;"
        queries.append(q)
    query = ';'.join(queries)
    result = api.query(query)
    return result


def compute_lead_score(row) -> int:
    score = 0
    score += max(0, 30 - row['newness_days'])
    if row['phone']:
        score += 10
    if row['email/social']:
        score += 5
    if row['income_tier'] == 'High':
        score += 10
    elif row['income_tier'] == 'Medium':
        score += 5
    return score


def main():
    init_db()

    st.title("Local Lead Generator")

    with st.sidebar:
        place = st.text_input("ZIP or Place", "10001")
        radius = st.selectbox("Radius (miles)", [10, 15, 25], index=0)
        vertical_options = list(VERTICAL_TAGS.keys())
        selected_verticals = st.multiselect("Target Verticals", vertical_options, default=vertical_options[:3])
        days = st.slider("New within N days", 1, 30, 14)
        refresh = st.button("Refresh Leads")
        if refresh:
            st.cache_data.clear()

    lat, lon = geocode_place(place)
    st.map(pd.DataFrame({"lat": [lat], "lon": [lon]}))

    result = overpass_query(lat, lon, radius, selected_verticals, days)

    income_df = load_income_data()
    called_ids = load_called_ids()

    leads = []
    for node in result.nodes:
        if 'website' in node.tags:
            continue
        phone = node.tags.get('phone') or node.tags.get('contact:phone')
        if not phone:
            continue
        opening = node.tags.get('opening_date') or node.tags.get('start_date')
        if not opening:
            continue
        try:
            newness = (datetime.utcnow() - datetime.strptime(opening, "%Y-%m-%d")).days
        except Exception:
            continue
        zip_code = node.tags.get('addr:postcode', '')
        income_row = income_df[income_df['zip'] == zip_code]
        if not income_row.empty:
            income = income_row.iloc[0]['median_income']
            if income >= 80000:
                tier = 'High'
            elif income >= 60000:
                tier = 'Medium'
            else:
                tier = 'Low'
        else:
            tier = 'Unknown'
        email = node.tags.get('email') or node.tags.get('contact:email')
        social = node.tags.get('contact:facebook') or node.tags.get('contact:instagram')
        name = node.tags.get('name', 'Unknown')
        industry = [k for k, v in VERTICAL_TAGS.items() if all(node.tags.get(tk) == tv for tk, tv in v.items())]
        industry = industry[0] if industry else 'Other'
        slug = slugify(name)
        demo_link = f"https://yourdomain.com/demo/{slug}"
        row = {
            'osm_id': str(node.id),
            'name': name,
            'industry': industry,
            'address': node.tags.get('addr:full', ''),
            'phone': phone,
            'email/social': email or social or '',
            'newness_days': newness,
            'income_tier': tier,
            'demo_slug': slug,
            'demo_link': demo_link
        }
        row['lead_score'] = compute_lead_score(row)
        leads.append(row)
        save_lead(str(node.id), row)

    df = pd.DataFrame(leads)

    filters = st.multiselect("Quick Filters", ['High Income', 'Not Called'])
    if 'High Income' in filters:
        df = df[df['income_tier'] == 'High']
    if 'Not Called' in filters:
        df = df[~df['osm_id'].isin(called_ids)]

    if df.empty:
        st.info("No leads found.")
        return

    df = df.sort_values('lead_score', ascending=False).head(50)
    st.write("### Leads")
    edited = st.data_editor(df, num_rows="dynamic")

    for idx, row in edited.iterrows():
        st.markdown(f"**{row['name']}**")
        st.write(f"📞 [Call]({f'tel:{quote_plus(row['phone'])}'})")
        outcome = st.selectbox("Outcome", ["Uncalled", "Connected", "Voicemail", "No Answer"], key=f"outcome_{row['osm_id']}")
        if outcome != "Uncalled":
            mark_call(row['osm_id'], outcome)
        st.write(f"[Demo Link]({row['demo_link']})")
        st.write("---")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Export to CSV"):
            df.to_csv("leads_export.csv", index=False)
            st.success("Exported to leads_export.csv")
    with col2:
        sms_template = f"Check out our demo: {df.iloc[0]['demo_link']}" if not df.empty else ''
        st.text_input("SMS Template", sms_template)


if __name__ == '__main__':
    main()
