"""
TuneWave — Music Distribution Dashboard
========================================
A Flask app where an artist can:
  - Upload tracks to their catalog (title, genre, release date, cover art)
  - View their full catalog in a table
  - See a dashboard with "real-time" performance analytics:
        * total streams, total revenue, catalog size
        * a streams-over-time chart (last 30 days)
        * a top-tracks-by-streams chart
        * a genre breakdown

Because we don't have access to a real streaming-platform API, analytics
are generated with a *deterministic simulation*: each track gets a daily
stream count derived from a seeded random walk based on its id and age.
This keeps the numbers stable between page loads (so it feels like real
data) while still being clearly a demo/prototype.
"""

import os
import random
import sqlite3
from datetime import date, datetime, timedelta, timezone

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "tunewave.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "covers")
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp"}
ANALYTICS_DAYS = 30
REVENUE_PER_STREAM = 0.004  # rough average payout per stream, in $

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = "tunewave-secret-key"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            artist TEXT NOT NULL,
            genre TEXT NOT NULL,
            release_date TEXT NOT NULL,
            cover_filename TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


# ---------------------------------------------------------------------------
# Analytics simulation
# ---------------------------------------------------------------------------
def simulate_daily_streams(track_id, release_date, days=ANALYTICS_DAYS):
    """
    Deterministically generate a list of (date, stream_count) for the last
    `days` days for a given track. Uses the track id as a random seed so
    the numbers stay stable across requests, but differ per track.

    Streams ramp up from a track's release date (no streams before release)
    and follow a gentle upward-trending random walk, simulating organic
    growth + day-to-day variation.
    """
    rng = random.Random(track_id * 7919)  # large prime for spread-out seeds
    base = rng.randint(40, 220)  # baseline daily streams once "warmed up"
    trend = rng.uniform(0.5, 3.0)  # average daily growth

    today = date.today()
    series = []
    level = max(base * 0.15, 5)

    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        if day < release_date:
            series.append((day, 0))
            continue

        noise = rng.uniform(0.7, 1.3)
        level = level + trend + rng.uniform(-trend, trend)
        level = max(level, 1)
        streams = int(level * noise)
        series.append((day, streams))

    return series


def build_analytics(tracks):
    """Aggregate per-track simulated streams into dashboard-wide stats."""
    per_track = {}
    totals_by_date = {}
    total_streams_all_time = 0

    for t in tracks:
        rd = date.fromisoformat(t["release_date"])
        series = simulate_daily_streams(t["id"], rd)
        per_track[t["id"]] = series

        for day, streams in series:
            totals_by_date[day] = totals_by_date.get(day, 0) + streams

        # rough "all-time" total: daily average * days since release (capped)
        days_live = max((date.today() - rd).days, 1)
        avg_recent = sum(s for _, s in series) / len(series) if series else 0
        total_streams_all_time += int(avg_recent * min(days_live, 365))

    timeline = [
        {"date": d.isoformat(), "streams": s}
        for d, s in sorted(totals_by_date.items())
    ]

    top_tracks = []
    for t in tracks:
        recent = per_track.get(t["id"], [])
        recent_total = sum(s for _, s in recent)
        top_tracks.append(
            {
                "id": t["id"],
                "title": t["title"],
                "genre": t["genre"],
                "streams_30d": recent_total,
            }
        )
    top_tracks.sort(key=lambda x: x["streams_30d"], reverse=True)

    genre_totals = {}
    for tt in top_tracks:
        genre_totals[tt["genre"]] = genre_totals.get(tt["genre"], 0) + tt["streams_30d"]

    streams_30d = sum(tt["streams_30d"] for tt in top_tracks)
    estimated_revenue = round(total_streams_all_time * REVENUE_PER_STREAM, 2)

    return {
        "timeline": timeline,
        "top_tracks": top_tracks,
        "genre_totals": genre_totals,
        "streams_30d": streams_30d,
        "total_streams_all_time": total_streams_all_time,
        "estimated_revenue": estimated_revenue,
        "per_track": per_track,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    conn = get_db()
    tracks = conn.execute("SELECT * FROM tracks ORDER BY release_date DESC").fetchall()
    conn.close()

    analytics = build_analytics(tracks)

    return render_template(
        "dashboard.html",
        tracks=tracks,
        analytics=analytics,
        track_count=len(tracks),
    )


@app.route("/catalog")
def catalog():
    conn = get_db()
    tracks = conn.execute("SELECT * FROM tracks ORDER BY release_date DESC").fetchall()
    conn.close()
    return render_template("catalog.html", tracks=tracks)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "GET":
        return render_template("upload.html")

    title = request.form.get("title", "").strip()
    artist = request.form.get("artist", "").strip()
    genre = request.form.get("genre", "").strip()
    release_date = request.form.get("release_date", "").strip()

    if not title or not artist or not genre or not release_date:
        flash("Please fill in all fields.", "error")
        return redirect(url_for("upload"))

    try:
        date.fromisoformat(release_date)
    except ValueError:
        flash("Invalid release date.", "error")
        return redirect(url_for("upload"))

    cover_filename = None
    file = request.files.get("cover")
    if file and file.filename:
        if not allowed_file(file.filename):
            flash("Cover art must be a PNG, JPG, or WEBP image.", "error")
            return redirect(url_for("upload"))
        safe_name = secure_filename(file.filename)
        unique_name = f"{datetime.now(timezone.utc).timestamp():.0f}_{safe_name}"
        file.save(os.path.join(UPLOAD_DIR, unique_name))
        cover_filename = unique_name

    conn = get_db()
    conn.execute(
        """INSERT INTO tracks (title, artist, genre, release_date, cover_filename, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (title, artist, genre, release_date, cover_filename, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    flash(f"'{title}' was added to your catalog.", "success")
    return redirect(url_for("dashboard"))


@app.route("/track/<int:track_id>")
def track_detail(track_id):
    conn = get_db()
    track = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    conn.close()

    if track is None:
        flash("Track not found.", "error")
        return redirect(url_for("catalog"))

    rd = date.fromisoformat(track["release_date"])
    series = simulate_daily_streams(track["id"], rd)
    total_30d = sum(s for _, s in series)
    avg_daily = round(total_30d / len(series), 1) if series else 0

    return render_template(
        "track_detail.html",
        track=track,
        series=series,
        total_30d=total_30d,
        avg_daily=avg_daily,
    )


@app.route("/delete/<int:track_id>", methods=["POST"])
def delete_track(track_id):
    conn = get_db()
    track = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if track and track["cover_filename"]:
        cover_path = os.path.join(UPLOAD_DIR, track["cover_filename"])
        if os.path.exists(cover_path):
            os.remove(cover_path)
    conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
    conn.commit()
    conn.close()
    flash("Track removed from catalog.", "success")
    return redirect(url_for("catalog"))


@app.route("/api/track/<int:track_id>/streams")
def api_track_streams(track_id):
    conn = get_db()
    track = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    conn.close()
    if track is None:
        return jsonify({"error": "not found"}), 404

    rd = date.fromisoformat(track["release_date"])
    series = simulate_daily_streams(track["id"], rd)
    return jsonify(
        {
            "labels": [d.isoformat() for d, _ in series],
            "streams": [s for _, s in series],
        }
    )


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5001)
