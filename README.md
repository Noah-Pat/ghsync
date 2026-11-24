# ghsync — Google Health (Fitbit Air) → SQLite

A small Docker container that polls the [Google Health API](https://developers.google.com/health/about) every 30 minutes and saves your data to `./data/health.db`. It's one Python file that uses only the standard library and runs on `python:3.13-alpine`.

## 1. Google auth setup (one time)

| Step | Link |
|---|---|
| Link your Fitbit to your Google account by signing in to the Google Health mobile app | [Setup guide](https://developers.google.com/health/setup) |
| Create a Google Cloud project | https://console.cloud.google.com/projectcreate |
| Enable **Google Health API** | https://console.cloud.google.com/apis/library/health.googleapis.com |
| Set up the consent screen: User type **External**, add yourself under **Test users** | https://console.cloud.google.com/auth/audience |
| Add scopes (search "Google Health API"; the `.readonly` ones for activity_and_fitness, health_metrics_and_measurements and sleep) | https://console.cloud.google.com/auth/scopes |
| Create the OAuth client: type **Desktop app**. It needs no redirect URI, because `http://localhost:<port>` is allowed automatically | https://console.cloud.google.com/auth/clients |
| Reference: scopes, data types, rate limits | [scopes](https://developers.google.com/health/scopes) · [data types](https://developers.google.com/health/data-types) · [rate limits](https://developers.google.com/health/rate-limits) |

> **7-day token expiry:** while the app's publishing status is **Testing**, Google refresh tokens expire after 7 days, and you'd need to run `login` again every week. Google's docs say unverified clients are capped at 100 users "for both testing and production". Clicking **Publish app** on the Audience page fixes this without a security review: after publishing and running `login` again, the token comes back with no expiry. Publishing first requires an app name, a support email, a homepage and a privacy policy on the Branding page. Expect an "unverified app" warning when you sign in; click **Advanced → Go to (app)**.

## 2. Configure and log in

```bash
cp .env.example .env        # paste in GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
```

```bash
docker compose run --rm ghsync login
```

Open the URL it prints and approve access. The browser then goes to `http://localhost:8765/?code=...` and shows an error page, which is expected. Copy that whole address and paste it into the prompt. The tool saves the token and prints your identity and paired devices.

## 3. Run

```bash
docker compose up -d --build
```

```bash
docker compose logs -f ghsync
```

- The first run downloads the last `BACKFILL_DAYS` days (default 30).
- After that, each run starts a little before where the previous one stopped (`LOOKBACK_HOURS`, default 48), so data from a late watch sync isn't missed. Records are upserted, so there are no duplicates.
- If one data type fails (for example the Air doesn't produce it), the error is logged and the other types keep syncing.

## Data

The data goes in one table, `data_points`, with these columns:

- `data_type` (`heart-rate`, `steps`, `sleep`, …)
- `start_time` and `end_time` (UTC RFC3339, or `YYYY-MM-DD` for daily types)
- `value`: the main number for the type (bpm, step count, minutes asleep, …)
- `source`
- `raw`: the rest of the API's JSON for that point, without the time fields, which already have their own columns

```sql
-- resting HR trend
SELECT start_time AS day, value AS bpm FROM data_points
WHERE data_type = 'daily-resting-heart-rate' ORDER BY day;

-- steps per day
SELECT substr(start_time,1,10) AS day, SUM(value) FROM data_points
WHERE data_type = 'steps' GROUP BY day;

-- sleep stages from the raw JSON
SELECT start_time, json_extract(raw, '$.summary.minutesAsleep') FROM data_points
WHERE data_type = 'sleep';
```

To use it in Grafana, install the [SQLite datasource plugin](https://grafana.com/grafana/plugins/frser-sqlite-datasource/) and mount `./data` into the Grafana container read-only.

Some types are stored as one value per day: `total-calories` (kcal) and `floors`, which is empty on the Fitbit Air because it has no altimeter. `sedentary-period` values are in minutes and `exercise` values in seconds of active time.

## Commands

`login` · `whoami` (check account link and devices) · `sync` (run once) · `run` (loop; the container's default)
