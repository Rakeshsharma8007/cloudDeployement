# Cloud-Based Monitoring and Performance Analysis System for Web Applications

A cloud-hosted monitoring and observability dashboard built with **Python, Flask, SQLite, Requests, and Chart.js**, designed to continuously monitor the availability, response time, HTTP errors, and GitHub Actions CI/CD workflows of a deployed web application (such as **Empty Classroom Finder** on Render).

## Features

- **Server Status Monitoring**: Real-time Online / Offline status with configurable check intervals.
- **Response Time Analytics**: Measures HTTP round-trip latency in milliseconds (`ms`) with min/avg/max tracking.
- **Uptime Calculation**: Computes overall availability percentage across all historical checks.
- **HTTP Error Tracking**: Captures non-2xx/3xx status codes, connection errors, and request timeouts.
- **Interactive Visualizations**: Chart.js response-time line graph and check-outcome doughnut chart.
- **Downtime & Recovery Alerts**: Automatic alert generation on state transitions (`DOWNTIME`, `RECOVERY`, `HIGH_LATENCY`) with optional Slack/Discord webhook notifications.
- **GitHub Actions Workflow Monitoring**: Tracks recent CI/CD workflow runs for the target repository via the GitHub REST API.
- **Persistent SQLite History**: Stores monitors, health checks, alerts, and workflow runs in SQLite.

## Project Structure

```text
.
├── app.py                  # Flask server, background scheduler, SQLite & API routes
├── requirements.txt        # Python dependencies (Flask, requests, gunicorn)
├── Procfile                # Gunicorn start command for Render
├── render.yaml             # Render Blueprint configuration
├── .env.example            # Environment variable template
├── templates/
│   └── index.html          # Monitoring dashboard UI
├── static/
│   ├── css/style.css       # Dark observability theme
│   └── js/dashboard.js     # Chart.js & live dashboard polling
└── tests/
    └── test_app.py         # Unit & API integration tests
```

## Local Setup

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment variables (optional)**:
   Copy `.env.example` to `.env` or set environment variables directly:
   - `TARGET_NAME`: Display name of the monitored app (default: `Empty Classroom Finder`)
   - `TARGET_URL`: Full `https://...` URL of your Render app
   - `CHECK_INTERVAL_SECONDS`: Polling interval in seconds (default: `60`)
   - `GITHUB_REPO`: Optional `owner/repo` for GitHub Actions monitoring

3. **Run the application**:
   ```bash
   python app.py
   ```
   Open `http://localhost:5000` in your browser. You can also update the target URL and GitHub repository directly from the **Configure Target** button in the dashboard.

4. **Run tests**:
   ```bash
   python -m unittest discover -s tests -v
   ```

## Deploying to Render

1. Push this repository to GitHub.
2. Log in to [Render](https://render.com) and click **New +** -> **Web Service** (or **Blueprint** using `render.yaml`).
3. Connect this GitHub repository.
4. Configure the service:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4`
   - **Health Check Path**: `/health`
5. Add Environment Variables in Render:
   - `TARGET_NAME` = `Empty Classroom Finder`
   - `TARGET_URL` = `<your-empty-classroom-finder-render-url>`
   - `CHECK_INTERVAL_SECONDS` = `60`
   - `GITHUB_REPO` = `<your-github-username>/<repo-name>` (optional)
   - `ALERT_WEBHOOK_URL` = `<discord-or-slack-webhook-url>` (optional)
6. Click **Create Web Service**.

> **Note on Render Free Tier**: External HTTP monitoring measures availability and HTTP response time from outside the target application. Render's free tier does not expose host-level CPU/RAM metrics, and free-tier SQLite files reset on service redeploy unless a persistent disk is attached.
