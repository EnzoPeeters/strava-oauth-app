"""
Strava OAuth callback app, structured for deployment on Render.

Reads secrets from environment variables (set these in Render's dashboard,
never commit them to the repo):
    STRAVA_CLIENT_ID
    STRAVA_CLIENT_SECRET
    REDIRECT_URI       (e.g. https://your-app-name.onrender.com/callback)
    DATABASE_URL        (Postgres connection string, e.g. from Neon)

Locally, you can still test this with:
    export STRAVA_CLIENT_ID=249346
    export STRAVA_CLIENT_SECRET=your_secret
    export REDIRECT_URI=http://localhost:8000/callback
    export DATABASE_URL=postgresql://...
    python app.py
"""

import os

import psycopg2
import requests
from flask import Flask, request

CLIENT_ID = os.environ["STRAVA_CLIENT_ID"]
CLIENT_SECRET = os.environ["STRAVA_CLIENT_SECRET"]
REDIRECT_URI = os.environ["REDIRECT_URI"]
DATABASE_URL = os.environ["DATABASE_URL"]
SCOPE = "activity:read_all"

app = Flask(__name__)


def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tokens (
                    athlete_id BIGINT PRIMARY KEY,
                    athlete_name TEXT,
                    refresh_token TEXT NOT NULL,
                    access_token TEXT NOT NULL,
                    expires_at BIGINT NOT NULL,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
                """
            )
        conn.commit()


def save_token_row(athlete_id, athlete_name, refresh_token, access_token, expires_at):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tokens (athlete_id, athlete_name, refresh_token, access_token, expires_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (athlete_id) DO UPDATE SET
                    athlete_name = EXCLUDED.athlete_name,
                    refresh_token = EXCLUDED.refresh_token,
                    access_token = EXCLUDED.access_token,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
                """,
                (athlete_id, athlete_name, refresh_token, access_token, expires_at),
            )
        conn.commit()


init_db()


@app.route("/")
def home():
    auth_url = (
        "https://www.strava.com/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        "&response_type=code"
        "&approval_prompt=auto"
        f"&scope={SCOPE}"
    )
    return (
        "<h2>Strava data collection</h2>"
        f'<p><a href="{auth_url}">Click here to connect your Strava account</a></p>'
    )


@app.route("/callback")
def callback():
    code = request.args.get("code")
    error = request.args.get("error")

    if error:
        return f"Authorization was denied or failed: {error}"

    if not code:
        return "No authorization code received. Something went wrong."

    resp = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
        },
    )

    if resp.status_code != 200:
        return f"Token exchange failed: {resp.status_code} {resp.text}"

    data = resp.json()
    athlete_id = data["athlete"]["id"]
    athlete_name = f"{data['athlete'].get('firstname', '')} {data['athlete'].get('lastname', '')}".strip()
    refresh_token = data["refresh_token"]
    access_token = data["access_token"]
    expires_at = data["expires_at"]

    save_token_row(athlete_id, athlete_name, refresh_token, access_token, expires_at)

    return (
        "<h2>Success!</h2>"
        f"<p>Authorized as: {athlete_name}</p>"
        "<p>You can close this tab now. Thank you!</p>"
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
