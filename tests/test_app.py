import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ["ENABLE_BACKGROUND_MONITOR"] = "false"

import app as monitor_app  # noqa: E402


class CloudMonitorTestCase(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        monitor_app.app.config["DB_PATH"] = self.db_path
        monitor_app.app.config["TESTING"] = True
        monitor_app.init_db(self.db_path)
        self.client = monitor_app.app.test_client()

    def tearDown(self):
        os.close(self.db_fd)
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_health_endpoint(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")

    def test_dashboard_page_renders(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Cloud Application Monitoring System", resp.data)

    @patch("app.requests.get")
    def test_health_check_up_down_recovery_alerts(self, mock_get):
        # 1. Successful check (UP)
        ok_resp = MagicMock(status_code=200, reason="OK")
        mock_get.return_value = ok_resp

        res1 = self.client.post("/api/check-now", json={"monitor_id": 1})
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.get_json()["status"], "UP")

        # 2. Failed check (HTTP 503 -> DOWN -> triggers DOWNTIME alert)
        err_resp = MagicMock(status_code=503, reason="Service Unavailable")
        mock_get.return_value = err_resp

        res2 = self.client.post("/api/check-now", json={"monitor_id": 1})
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.get_json()["status"], "DOWN")

        # 3. Recovery check (HTTP 200 -> UP -> triggers RECOVERY alert)
        mock_get.return_value = ok_resp
        res3 = self.client.post("/api/check-now", json={"monitor_id": 1})
        self.assertEqual(res3.status_code, 200)
        self.assertEqual(res3.get_json()["status"], "UP")

        # Verify dashboard summary & alerts
        dash = self.client.get("/api/dashboard?monitor_id=1").get_json()
        summary = dash["summary"]
        self.assertEqual(summary["total_checks"], 3)
        self.assertEqual(summary["successful_checks"], 2)
        self.assertEqual(summary["failed_checks"], 1)
        self.assertEqual(summary["uptime_percentage"], 66.67)
        self.assertEqual(len(dash["error_history"]), 1)
        self.assertEqual(len(dash["alerts"]), 2)
        self.assertEqual(dash["alerts"][0]["alert_type"], "RECOVERY")
        self.assertEqual(dash["alerts"][1]["alert_type"], "DOWNTIME")

    def test_create_and_update_monitor(self):
        create_resp = self.client.post(
            "/api/monitors",
            json={
                "name": "API Gateway",
                "url": "https://example.com/health",
                "check_interval": 30,
                "github_repo": "octocat/Hello-World",
            },
        )
        self.assertEqual(create_resp.status_code, 201)
        created = create_resp.get_json()
        self.assertEqual(created["name"], "API Gateway")

        update_resp = self.client.put(
            f"/api/monitors/{created['id']}",
            json={"check_interval": 45},
        )
        self.assertEqual(update_resp.status_code, 200)
        self.assertEqual(update_resp.get_json()["check_interval"], 45)

    @patch("app.requests.get")
    def test_sync_github_workflows(self, mock_get):
        self.client.put(
            "/api/monitors/1",
            json={"github_repo": "octocat/Hello-World"},
        )
        wf_mock = MagicMock(status_code=200)
        wf_mock.json.return_value = {
            "workflow_runs": [
                {
                    "id": 9991,
                    "name": "CI Build",
                    "run_number": 12,
                    "status": "completed",
                    "conclusion": "success",
                    "head_branch": "main",
                    "event": "push",
                    "html_url": "https://github.com/octocat/Hello-World/actions/runs/9991",
                    "created_at": "2026-11-25T05:00:00Z",
                    "updated_at": "2026-11-25T05:01:00Z",
                }
            ]
        }
        mock_get.return_value = wf_mock

        sync_resp = self.client.post("/api/workflows/sync", json={"monitor_id": 1})
        self.assertEqual(sync_resp.status_code, 200)
        self.assertTrue(sync_resp.get_json()["ok"])

        dash = self.client.get("/api/dashboard?monitor_id=1").get_json()
        self.assertEqual(len(dash["workflows"]), 1)
        self.assertEqual(dash["workflows"][0]["workflow_name"], "CI Build")


if __name__ == "__main__":
    unittest.main()
