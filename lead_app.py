import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import pandas as pd
import streamlit as st
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable
import overpy
import gspread
from google.oauth2.service_account import Credentials

# --- Constants & Config ---
INCOME_CSV = os.path.join('data', 'zip_income_sample.csv')
CACHE_DB = 'lead_cache.db'
VERTICAL_TAGS = {
    'Plumbing': {'craft': 'plumber'},
    'Cafe': {'amenity': 'cafe'},
    'Pet Grooming': {'shop': 'pet'},
    'Medical Clinic': {'amenity': 'clinic'},
    'Specialty Retail': {'shop': 'electronics'}
}

# --- Helpers & Persistence ---
def slugify(value: str) -> str:
    return ''.join(c if c.isalnum() else '-' for c in value.lower()).strip('-')

def validate_zip(zip_code: str) -> str:
    """Return ZIP if valid else stop the app with an error."""
    if not re.fullmatch(r"\d{5}", zip_code.strip()):
        st.error("Please enter a valid 5-digit U.S. ZIP code.")
        st.stop()
    return zip_code.strip()

def init_db():
    conn = sqlite3.connect(CACHE_DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS leads (
                    osm_id TEXT PRIMARY KEY,
                    data TEXT
                 )""")
    c.execute("""CREATE TABLE IF NOT EXISTS calls (
                    osm_id TEXT,
                    outcome TEXT,
                    timestamp TEXT
                 )""")
    conn.commit()
    conn.close()

def save_lead(osm_id: str, data: dict):
    conn = sqlite3.connect(CACHE_DB)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO leads(osm_id, data) VALUES(?, ?)",
              (osm_id, json.dumps(data)))
    conn.commit()
    conn.close()

def mark_call(osm_id: str, outcome: str):
    conn = sqlite3.connect(CACHE_DB)
    c = conn.cursor()
    c.execute("INSERT INTO calls(osm_id, outcome, timestamp) VALUES(?, ?, ?)",
              (osm_id, outcome, datetime.utcnow().isoformat()))
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

# --- Geocoding & Caching ---
@st.cache_data(show_spinner=False, ttl=24*3600)
def geocode_zip(zip_code: str):
    geolocator = Nominatim(user_agent="streamlit-lead-app")
    query = f"{zip_code}, USA"
    for _ in range(3):
        try:
            loc = geolocator.geocode(query, timeout=10)
            if loc:
                return loc.latitude, loc.longitude
        except (GeocoderTimedOut, GeocoderUnavailable):
            time.sleep(1)
    st.error(f"❌ Could not geocode '{zip_code}'. Try a different ZIP code.")
    st.stop()

# --- Overpass Query & Caching ---
@st.cache_data(show_spinner=False, ttl=24*3600)
def overpass_query(lat: float, lon: float, radius: int, verticals: list, days: int):
    api = overpy.Overpass()
    date_limit = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    parts = []
    for name in verticals:
        tags = VERTICAL_TAGS[name]
        for k, v in tags.items():
            for elem in ("node", "way"):
                for date_tag in ("opening_date", "start_date"):
                    parts.append(
                        f'  {elem}[{k}="{v}"](around:{radius*1609},{lat},{lon})["{date_tag}" > "{date_limit}"];'
                    )
    combined = "\n".join(parts)
    query = f"""
[out:json][timeout:25];
(
{combined}
);
out center;
"""
    try:
        return api.query(query)
    except overpy.exception.OverpassBadRequest:
        st.error("⚠️ Overpass query failed. Try a wider radius or shorter date range.")
        st.stop()

# --- Lead Scoring ---
def compute_lead_score(row) -> int:
    score = max(0, 30 - row['newness_days'])
    if row['phone']:
        score += 10
    if row['email/social']:
        score += 5
    if row['income_tier'] == 'High':
        score += 10
    elif row['income_tier'] == 'Medium':
        score += 5
    return score

# --- Main App ---
def main():
    init_db()
    st.title("Local Lead Generator")

    # Sidebar controls
    with st.sidebar:
        zip_input = st.text_input("U.S. ZIP Code", "10001")
        radius = st.selectbox("Radius (miles)", [10, 15, 25], index=0)
        verticals = st.multiselect(
            "Target Verticals",
            list(VERTICAL_TAGS.keys()),
            default=list(VERTICAL_TAGS.keys())[:3]
        )
        days = st.slider("New within N days", 1, 30, 14)
        if st.button("Refresh Leads"):
            st.cache_data.clear()

    # Geocode & display map
    zip_code = validate_zip(zip_input)
    lat, lon = geocode_zip(zip_code)
    st.map(pd.DataFrame({"lat": [lat], "lon": [lon]}))

    # Fetch POIs and process
    result = overpass_query(lat, lon, radius, verticals, days)
    income_df = load_income_data()
    called_ids = load_called_ids()

    leads = []
    elements = list(result.nodes) + list(result.ways)
    for el in elements:
        tags = el.tags
        if 'website' in tags:
            continue
        phone = tags.get('phone') or tags.get('contact:phone')
        if not phone:
            continue
        opening = tags.get('opening_date') or tags.get('start_date')
        if not opening:
            continue
        try:
            newness = (datetime.utcnow() - datetime.fromisoformat(opening)).days
        except Exception:
            continue
        if str(el.id) in called_ids:
            continue

        zip_lookup = tags.get('addr:postcode', '')
        inc_row = income_df[income_df['zip'] == zip_lookup]
        if not inc_row.empty:
            inc = inc_row.iloc[0]['median_income']
            tier = 'High' if inc >= 80000 else 'Medium' if inc >= 60000 else 'Low'
        else:
            tier = 'Unknown'

        email = tags.get('email') or tags.get('contact:email')
        social = tags.get('contact:facebook') or tags.get('contact:instagram')
        name = tags.get('name', 'Unknown')
        industry = next(
            (k for k, v in VERTICAL_TAGS.items()
             if all(tags.get(tk) == tv for tk, tv in v.items())),
            'Other'
        )
        slug = slugify(name)
        demo_link = f"https://yourdomain.com/demo/{slug}"

        addr = tags.get('addr:full') or ' '.join(
            filter(None, [tags.get('addr:housenumber'), tags.get('addr:street'), tags.get('addr:city'), tags.get('addr:state')])
        )

        row = {
            'osm_id': str(el.id),
            'name': name,
            'industry': industry,
            'address': addr,
            'phone': phone,
            'email/social': email or social or '',
            'newness_days': newness,
            'income_tier': tier,
            'demo_link': demo_link
        }
        row['lead_score'] = compute_lead_score(row)
        leads.append(row)
        save_lead(str(el.id), row)

    df = pd.DataFrame(leads)

    if df.empty:
        st.info("No leads found.")
        return

    if st.checkbox("Show only High Income ZIPs"):
        df = df[df['income_tier'] == 'High']

    df = df.sort_values('lead_score', ascending=False).head(50)
    st.write("### Leads")
    edited = st.data_editor(df, num_rows="dynamic")

    # Call & log
    for _, row in edited.iterrows():
        st.markdown(f"**{row['name']}**")
        st.write(f"📞 [Call](tel:{quote_plus(row['phone'])})")
        outcome = st.selectbox(
            "Outcome",
            ["Uncalled", "Connected", "Voicemail", "No Answer"],
            key=f"outcome_{row['osm_id']}"
        )
        if outcome != "Uncalled":
            mark_call(row['osm_id'], outcome)
        st.write(f"[Demo Link]({row['demo_link']})")
        st.write("---")

    # Export & SMS
    col1, col2 = st.columns(2)
    with col1:
        csv_data = df.to_csv(index=False).encode("utf-8")
        st.download_button("Export to CSV", csv_data, "leads.csv", "text/csv")
        if st.button("Export to Google Sheets"):
            try:
                info = st.secrets.get("gcp_service_account")
                if not info:
                    st.error("Add Google service account credentials to secrets to enable export.")
                else:
                    creds = Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
                    client = gspread.authorize(creds)
                    sheet = client.create("Lead Export")
                    sheet.sheet1.update([df.columns.tolist()] + df.astype(str).values.tolist())
                    sheet.share(None, perm_type="anyone", role="writer")
                    st.success(f"Sheet created: {sheet.url}")
            except Exception as e:
                st.error(f"Google Sheets export failed: {e}")
    with col2:
        sms = f"Hi, check out our demo: {df.iloc[0]['demo_link']}" if not df.empty else ""
        if st.button("Copy SMS Template"):
            st.code(sms)

if __name__ == '__main__':
    main()
