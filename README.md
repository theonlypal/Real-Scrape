# Streamlit Lead Generation App

This repository contains a simple Streamlit application for discovering brand new local businesses without websites. It uses only free and freemium data sources.

## Features
- Enter a ZIP code or place name, search radius, target verticals, and a date range of recently opened businesses.
- Geocodes locations with OpenStreetMap Nominatim and queries business data from the Overpass API.
- Filters out listings that already have a website and keeps only entries with phone numbers.
- Enriches leads with a sample median household income file from the U.S. Census and scores each lead.
- Persists leads locally so duplicates are hidden on the next run.
- Click-to-call links, outcome logging, SMS templates, and CSV export.

## Running Locally
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Run the application:
   ```bash
   streamlit run lead_app.py
   ```
3. Use the sidebar to configure the search parameters and start calling your top prospects.

## Deployment
Deploy this repo for free on [Streamlit Community Cloud](https://streamlit.io/cloud) by linking it to your public GitHub repository.

## GitHub Actions
A GitHub Actions workflow can be added to automatically export fresh leads each day. See the commented section in `lead_app.py` as a starting point.
