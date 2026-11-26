from datetime import datetime, timezone
from pathlib import Path
import unittest

from app.api.frontend_api import _alert


ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "app/dashboard/static/dashboard.html").read_text(encoding="utf-8")


class DashboardAlertCenterTests(unittest.TestCase):
    def test_alert_response_serialization_includes_system_and_both_time_meanings(self):
        row = {
            "id": 16533,
            "rule_id": "HVAC_HIGH_TEMP",
            "detection_method": "threshold",
            "severity": "warning",
            "status": "open",
            "title": "manual test",
            "summary": "manual test summary",
            "first_ts": datetime(2026, 11, 11, 12, 40, 20, tzinfo=timezone.utc),
            "last_ts": datetime(2026, 11, 11, 12, 50, 0, tzinfo=timezone.utc),
            "detected_ts": datetime(2026, 11, 22, 11, 22, 21, tzinfo=timezone.utc),
            "incident_id": 17,
            "asset_id": 1,
            "asset_name": "Air Handler 1",
            "zone": "Roof Plant",
            "system": "HVAC",
        }

        result = _alert(row)

        self.assertEqual(result["id"], 16533)
        self.assertEqual(result["system_type"], "HVAC")
        self.assertEqual(result["severity"], "WARNING")
        self.assertEqual(result["status"], "OPEN")
        self.assertEqual(result["first_ts"], "2026-11-11T12:40:20Z")
        self.assertEqual(result["last_ts"], "2026-11-11T12:50:00Z")
        self.assertEqual(result["detected_ts"], "2026-11-22T11:22:21Z")
        self.assertEqual(result["incident_id"], 17)

    def test_alert_center_filters_system_and_severity_but_not_dashboard_time(self):
        start = HTML.index("    const passes = alert => {")
        end = HTML.index("    /* =========================================================", start)
        predicate = HTML[start:end]

        self.assertIn("(!AC.filters.severity || alert.severity === AC.filters.severity)", predicate)
        self.assertIn("(!system || !state.system || system === state.system)", predicate)
        self.assertIn("(!system || !AC.filters.system || system === AC.filters.system)", predicate)
        self.assertNotIn("inWindow", predicate)
        self.assertIn('"Unknown system"', HTML)

    def test_alert_card_distinguishes_detection_time_from_evidence_window(self):
        start = HTML.index("    function renderAlerts() {")
        end = HTML.index("    /* =========================================================", start)
        renderer = HTML[start:end]

        self.assertIn("alert.detected_ts", renderer)
        self.assertIn("Detected ", renderer)
        self.assertIn("Evidence window:", renderer)
        self.assertIn("alertEvidenceWindow(alert)", renderer)
        self.assertIn("alert.first_ts", HTML[HTML.index("const alertEvidenceWindow"):start])
        self.assertIn("alert.last_ts", HTML[HTML.index("const alertEvidenceWindow"):start])

    def test_alert_loader_reports_unexpected_response_shape(self):
        start = HTML.index("    async function loadInitialAlerts()")
        end = HTML.index("    /* =========================================================", start)
        loader = HTML[start:end]

        self.assertIn("!Array.isArray(data.items)", loader)
        self.assertIn("Alerts API response does not contain an items array.", loader)
        self.assertNotIn("? data.items : []", loader)
        self.assertIn("AC.loadError = true", loader)

    def test_socket_reconnect_backfills_by_alert_id_without_replaying_live_side_effects(self):
        start = HTML.index("    async function backfillAlerts() {")
        end = HTML.index("    /* =========================================================", start)
        backfill = HTML[start:end]
        self.assertIn("/api/v1/alerts/stream?after_id=", backfill)
        self.assertIn("onNewAlert(alert, false)", backfill)
        self.assertIn("data.next_after_id", backfill)
        self.assertIn("data.has_more", backfill)
        connect_start = HTML.index('alertSocket.on("connect", () => {')
        connect_end = HTML.index('alertSocket.on("disconnect"', connect_start)
        self.assertIn("void backfillAlerts()", HTML[connect_start:connect_end])

    def test_initial_cursor_is_global_and_socket_alerts_advance_it(self):
        self.assertIn("/api/v1/alerts/stream/latest", HTML)
        self.assertIn("AC.lastAlertId = Math.max(AC.lastAlertId, alertId)", HTML)
        self.assertIn("AC.cursorInitialized", HTML)

    def test_alert_replay_does_not_advance_from_visible_rows_when_watermark_fails(self):
        live_start = HTML.index("    function onNewAlert(alert, isLive = true) {")
        live_end = HTML.index("    /* =========================================================", live_start)
        live_handler = HTML[live_start:live_end]
        load_start = HTML.index("    async function loadInitialAlerts() {")
        load_end = HTML.index("    async function backfillAlerts() {", load_start)
        loader = HTML[load_start:load_end]
        backfill_start = load_end
        backfill_end = HTML.index("    function setAlertStatus", backfill_start)
        backfill = HTML[backfill_start:backfill_end]

        self.assertIn("if (AC.cursorInitialized) AC.lastAlertId = Math.max(AC.lastAlertId, alertId)", live_handler)
        self.assertNotIn("AC.cursorInitialized = true", loader)
        self.assertNotIn("AC.lastAlertId = Math.max(AC.lastAlertId, ...loadedIds)", loader)
        self.assertIn("throw new Error(\"Cannot replay alerts until the global cursor is available.\")", backfill)

    def test_incident_list_remains_filtered_by_system_and_selected_window(self):
        start = HTML.index("    function incMatches(i) {")
        end = HTML.index("    function renderIncidentList()", start)
        predicate = HTML[start:end]

        self.assertIn("AC.sysById.get(id)", predicate)
        self.assertIn("systems.includes(state.system)", predicate)
        self.assertIn("inWindow(i.first_ts, i.last_ts)", predicate)
        self.assertIn("linkedAlerts.some(alert => alertDetectedAfterWindow(alert))", predicate)
        self.assertIn("return inWindow(i.first_ts, i.last_ts) ||", predicate)

    def test_incident_can_match_on_linked_alert_detection_after_window(self):
        start = HTML.index("    function alertDetectedAfterWindow(alert, now = Date.now())")
        end = HTML.index("    function incMatches(i)", start)
        helper = HTML[start:end]

        self.assertIn("detectedAt > state.windowTo", helper)
        self.assertIn("detectedAt <= now", helper)
        self.assertIn("Number.isFinite(detectedAt)", helper)

        window_end = datetime(2026, 11, 16, 21, 45, tzinfo=timezone.utc)
        actual_now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
        linked_alert_detection = datetime(2026, 11, 22, 11, 22, tzinfo=timezone.utc)
        future_alert_detection = datetime(2026, 11, 23, 11, 22, tzinfo=timezone.utc)
        incident = {"id": 17, "first_ts": "2026-11-11T12:40:02Z", "last_ts": "2026-11-11T12:50:00Z", "alert_ids": [16533]}
        linked_alert = {"id": 16533, "incident_id": 17, "detected_ts": "2026-11-22T11:22:21.945382Z"}

        self.assertEqual(incident["id"], linked_alert["incident_id"])
        self.assertEqual(incident["alert_ids"], [linked_alert["id"]])
        self.assertGreater(linked_alert_detection, window_end)
        self.assertLessEqual(linked_alert_detection, actual_now)
        self.assertGreater(future_alert_detection, actual_now)
        self.assertEqual(
            datetime.fromisoformat(linked_alert["detected_ts"].replace("Z", "+00:00")),
            datetime(2026, 11, 22, 11, 22, 21, 945382, tzinfo=timezone.utc),
        )

    def test_nonmatching_incident_stays_hidden_and_missing_ids_are_safe(self):
        start = HTML.index("    const inWindow = (first, last) => {")
        end = HTML.index("    const sysOf =", start)
        helper = HTML[start:end]
        linked_start = HTML.index("    function linkedAlertsFor(incident) {")
        linked_end = HTML.index("    function alertDetectedAfterWindow", linked_start)
        linked_helper = HTML[linked_start:linked_end]

        self.assertIn("if (!Number.isFinite(start) && !Number.isFinite(end)) return false;", helper)
        self.assertIn("filter(id => id != null)", linked_helper)
        self.assertIn(".filter(Boolean)", linked_helper)

        window_start = datetime(2026, 11, 14, 21, 45, tzinfo=timezone.utc)
        window_end = datetime(2026, 11, 16, 21, 45, tzinfo=timezone.utc)
        old_first = datetime(2026, 11, 11, 12, 40, tzinfo=timezone.utc)
        old_last = datetime(2026, 11, 11, 12, 50, tzinfo=timezone.utc)
        future_detection = datetime(2026, 11, 23, 11, 22, tzinfo=timezone.utc)
        actual_now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)

        evidence_match = old_last >= window_start and old_first <= window_end
        detected_match = window_end < future_detection <= actual_now
        self.assertFalse(evidence_match)
        self.assertFalse(detected_match)

    def test_rendered_incidents_are_deduplicated_before_counting(self):
        start = HTML.index("    function renderIncidentList() {")
        end = HTML.index("    async function selectIncident", start)
        renderer = HTML[start:end]

        self.assertIn("const unique = new Map();", renderer)
        self.assertIn("!unique.has(String(incident.id))", renderer)
        self.assertIn("const items = all.filter(incMatches);", renderer)
        self.assertIn("const openN = items.filter", renderer)


if __name__ == "__main__":
    unittest.main()
