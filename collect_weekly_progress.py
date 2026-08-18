"""
Daily collector: for every authorized athlete, pulls all activities from
the start of the current ISO week up to now, sums moving_time, and:
  1. Upserts the running total into a `weekly_totals` table in Postgres
  2. Writes/overwrites one output file per person per week:
     data/{athlete_id}/{iso_year}-W{week_number}.json

Meant to be run once a day (see the GitHub Actions workflow for
scheduling). Each run recomputes the current week's total from scratch,
so it's safe to run as often as you like — no double-counting.

Requires the same environment variables as app.py:
    STRAVA_CLIENT_ID
    STRAVA_CLIENT_SECRET
    DATABASE_URL
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import requests

CLIENT_ID = os.environ["STRAVA_CLIENT_ID"]
CLIENT_SECRET = os.environ["STRAVA_CLIENT_SECRET"]
DATABASE_URL = os.environ["DATABASE_URL"]

OUTPUT_DIR = "data"


def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def ensure_weekly_totals_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS weekly_totals (
                athlete_id BIGINT NOT NULL,
                iso_week TEXT NOT NULL,
                week_start DATE NOT NULL,
                total_moving_time_seconds INT NOT NULL,
                activity_count INT NOT NULL,
                last_updated TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (athlete_id, iso_week)
            )
            """
        )
    conn.commit()


def ensure_activities_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS activities (
                strava_activity_id BIGINT PRIMARY KEY,
                athlete_id BIGINT NOT NULL,
                sport_type TEXT,
                moving_time_seconds INT NOT NULL,
                start_date TIMESTAMP,
                iso_week TEXT NOT NULL,
                last_updated TIMESTAMP DEFAULT NOW()
            )
            """
        )
    conn.commit()


def fetch_all_athletes(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT athlete_id, athlete_name, refresh_token FROM tokens")
        return cur.fetchall()


def refresh_access_token(refresh_token):
    resp = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"], data["refresh_token"]


def get_week_bounds(now=None):
    now = now or datetime.now(timezone.utc)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return week_start, now


def get_activities_in_range(access_token, after_ts, before_ts):
    activities = []
    page = 1
    while True:
        resp = requests.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"after": after_ts, "before": before_ts, "per_page": 100, "page": page},
        )
        if resp.status_code == 429:
            print("  Rate limited, waiting 60s...")
            time.sleep(60)
            continue
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        activities.extend(batch)
        page += 1
    return activities


def upsert_activities(conn, athlete_id, iso_week, activities):
    with conn.cursor() as cur:
        for a in activities:
            cur.execute(
                """
                INSERT INTO activities
                    (strava_activity_id, athlete_id, sport_type, moving_time_seconds, start_date, iso_week, last_updated)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (strava_activity_id) DO UPDATE SET
                    sport_type = EXCLUDED.sport_type,
                    moving_time_seconds = EXCLUDED.moving_time_seconds,
                    start_date = EXCLUDED.start_date,
                    last_updated = NOW()
                """,
                (
                    a["id"],
                    athlete_id,
                    a.get("sport_type", a.get("type")),
                    a["moving_time"],
                    a.get("start_date"),
                    iso_week,
                ),
            )
    conn.commit()


def upsert_weekly_total(conn, athlete_id, iso_week, week_start_date, total_seconds, activity_count):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO weekly_totals
                (athlete_id, iso_week, week_start, total_moving_time_seconds, activity_count, last_updated)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (athlete_id, iso_week) DO UPDATE SET
                total_moving_time_seconds = EXCLUDED.total_moving_time_seconds,
                activity_count = EXCLUDED.activity_count,
                last_updated = NOW()
            """,
            (athlete_id, iso_week, week_start_date, total_seconds, activity_count),
        )
    conn.commit()


def write_output_file(athlete_id, iso_week, total_seconds, activity_count, by_type):
    person_dir = os.path.join(OUTPUT_DIR, str(athlete_id))
    os.makedirs(person_dir, exist_ok=True)
    filepath = os.path.join(person_dir, f"{iso_week}.json")
    with open(filepath, "w") as f:
        json.dump(
            {
                "athlete_id": athlete_id,
                "week": iso_week,
                "total_moving_time_seconds": total_seconds,
                "activity_count": activity_count,
                "by_type": by_type,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            f,
            indent=2,
        )


def main():
    conn = get_db_connection()
    ensure_weekly_totals_table(conn)
    ensure_activities_table(conn)

    athletes = fetch_all_athletes(conn)
    print(f"Found {len(athletes)} authorized athlete(s).")

    week_start, now = get_week_bounds()
    iso_week = week_start.strftime("%G-W%V")
    after_ts = int(week_start.timestamp())
    before_ts = int(now.timestamp())

    for athlete_id, athlete_name, refresh_token in athletes:
        print(f"Processing {athlete_name} ({athlete_id})...")
        try:
            access_token, new_refresh_token = refresh_access_token(refresh_token)

            if new_refresh_token != refresh_token:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE tokens SET refresh_token = %s WHERE athlete_id = %s",
                        (new_refresh_token, athlete_id),
                    )
                conn.commit()

            activities = get_activities_in_range(access_token, after_ts, before_ts)
            total_seconds = sum(a["moving_time"] for a in activities)
            activity_count = len(activities)

            by_type = {}
            for a in activities:
                sport = a.get("sport_type", a.get("type", "Unknown"))
                by_type[sport] = by_type.get(sport, 0) + a["moving_time"]

            upsert_activities(conn, athlete_id, iso_week, activities)
            upsert_weekly_total(
                conn, athlete_id, iso_week, week_start.date(), total_seconds, activity_count
            )
            write_output_file(athlete_id, iso_week, total_seconds, activity_count, by_type)

            print(f"  {activity_count} activities, {total_seconds}s total moving time.")

        except Exception as e:
            print(f"  FAILED for {athlete_name} ({athlete_id}): {e}")
            continue

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
