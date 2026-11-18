import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "searcher"))
import job_alerts as app


class JobAlertsTests(unittest.TestCase):
    def setUp(self):
        self.current = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
        self.listing = {
            "id": 123, "title": ".NET Backend Engineer", "company_name": "Example",
            "url": "https://remotive.com/remote-jobs/software/example-123",
            "publication_date": "2026-11-17T12:00:00", "candidate_required_location": "Europe",
            "description": "Build ASP.NET Core APIs", "job_type": "full_time",
        }

    def test_role_location_and_age_filters(self):
        self.assertEqual(app.normalize_job("Remotive", self.listing, self.current)["category"], ".NET engineering")
        for update in (
            {"candidate_required_location": "USA"},
            {"candidate_required_location": "Europe except Portugal"},
            {"publication_date": "2026-10-01T12:00:00Z"},
            {"title": "Senior .NET Engineer"},
            {"url": "https://evil.example/123"},
        ):
            with self.subTest(update=update):
                self.assertIsNone(app.normalize_job("Remotive", {**self.listing, **update}, self.current))
        self.assertEqual(app.classify("AI Engineering Intern", "", "", "internship"), "AI internship")
        self.assertIsNone(app.classify("Software Engineer", "We use AI tools", "", ""))
        local = {"slug": "1", "title": "AI Engineer", "company_name": "Example",
                 "url": "https://www.arbeitnow.com/jobs/1", "created_at": self.current.timestamp(),
                 "location": "Porto", "remote": False}
        self.assertEqual(app.normalize_job("Arbeitnow", local, self.current)["work_mode"], "Local (hybrid unconfirmed)")
        self.assertIsNone(app.normalize_job("Arbeitnow", {**local, "location": "Porto, Italy"}, self.current))

    def test_scan_delivery_deduplication_and_retry(self):
        job = app.normalize_job("Remotive", self.listing, self.current)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            with patch.object(app, "now", return_value=self.current), patch.object(app, "get_sources", return_value=({"Remotive": [job]}, {})):
                self.assertEqual(app.scan(path), 0)
                self.assertEqual(app.scan(path), 0)
                state = json.loads((path / "state.json").read_text())
                self.assertEqual(len(state["pending"]), 1)
                with patch.object(app, "fetch_json", side_effect=OSError("network")):
                    self.assertEqual(app.deliver(path, "token", "chat"), 1)
                self.assertEqual(len(json.loads((path / "state.json").read_text())["pending"]), 1)
                with patch.object(app, "fetch_json", return_value={"ok": True}) as send, patch.object(app.time, "sleep"):
                    self.assertEqual(app.deliver(path, "token", "chat"), 0)
                    self.assertEqual(send.call_count, 1)
                self.assertEqual(app.scan(path), 0)
                self.assertEqual(json.loads((path / "state.json").read_text())["pending"], [])

    def test_failed_source_keeps_previous_results(self):
        job = app.normalize_job("Remotive", self.listing, self.current)
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "now", return_value=self.current):
            path = Path(tmp)
            with patch.object(app, "get_sources", return_value=({"Remotive": [job]}, {})):
                app.scan(path)
            with patch.object(app, "get_sources", return_value=({"Arbeitnow": []}, {"Remotive": "offline"})):
                self.assertEqual(app.scan(path), 0)
            feed = json.loads((path / "jobs.json").read_text())
            self.assertEqual(feed["jobs"][0]["id"], job["id"])
            self.assertFalse(feed["sources"]["Remotive"]["ok"])

    def test_jobicy_closed_listing_is_not_alerted(self):
        job = {"id": "jobicy:1", "source": "Jobicy", "published_at": "2026-11-17T12:00:00Z"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            app.write_json(path / "state.json", {"sent": {}, "pending": [job]})
            with patch.object(app, "fetch_json", return_value={"success": True, "jobs": [{"id": 1, "status": "closed"}]}) as fetch:
                self.assertEqual(app.deliver(path, "token", "chat"), 0)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(json.loads((path / "state.json").read_text())["pending"], [])


if __name__ == "__main__":
    unittest.main()
