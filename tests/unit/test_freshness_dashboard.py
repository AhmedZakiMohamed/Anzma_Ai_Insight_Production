from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "app/dashboard/static/dashboard.html").read_text(encoding="utf-8")
HTML_NORMALIZED = re.sub(r"\s+", " ", HTML)


class FreshnessDashboardTests(unittest.TestCase):
    def setUp(self):
        self.start = HTML.index("    async function loadFreshness()")
        self.end = HTML.index("    /* =========================================================", self.start)
        self.renderer = HTML[self.start:self.end]
        helper_start = HTML.index("    function codeValue(value)")
        self.code_helper = HTML[helper_start:HTML.index("    async function loadFreshness()", helper_start)]

    def test_freshness_panel_is_additive_and_describes_evidence_only(self):
        self.assertIn('<section id="freshnessPanel"', HTML)
        self.assertIn("Data freshness &amp; sources", HTML)
        self.assertIn("Seed/legacy series have no source or building code; the rest of the dashboard measures time from the latest stored record, this panel from the current clock.", HTML_NORMALIZED)
        self.assertIn("Source SILENT and gap SOURCE_SUSPECTED labels are evidence", HTML)
        self.assertIn('api("/api/v1/freshness?limit=100")', HTML)
        self.assertIn('api("/api/v1/sources/liveness?limit=100")', HTML)
        self.assertIn('api("/api/v1/data-gaps?status=open&limit=100")', HTML)
        self.assertIn('id="freshnessStaleBanner"', HTML)
        self.assertIn('new Set(["ALIVE", "SILENT", "UNKNOWN", "DISABLED"])', HTML)
        self.assertIn("No ingestion sources registered yet — legacy/seed data has none", self.renderer)
        self.assertIn('$("refresh").onclick = () => { load(); loadIncidents(); };', HTML)
        self.assertNotIn("setInterval(loadFreshness", HTML)

    def test_dynamic_freshness_values_are_escaped_before_inner_html(self):
        for expression in ("${codeValue(item.building_code)}", "${codeValue(item.source_code)}", "${codeValue(item.asset_code)}",
                           "${codeValue(source.building_code)}", "${codeValue(source.source_code)}",
                           "${codeValue(gap.building_code)}", "${codeValue(gap.source_code)}", "${codeValue(gap.asset_code)}",
                           "${esc(item.freshness)}", "${esc(source.connector_status ? JSON.stringify(source.connector_status) : \"—\")}",
                           "${esc(source.last_error || \"—\")}", "${esc(source.reason_message)}",
                           "${esc(source.mass_stale ? \"yes\" : \"no\")}", "${esc(gap.cause)}", "textContent = `Snapshot:"):
            with self.subTest(expression=expression):
                self.assertIn(expression, self.renderer)
        self.assertIn('return esc(normalized || "—");', self.code_helper)
        self.assertIn('replace(/[&<>\"]/g, char => ({', HTML)
        for escaped in ('"&": "&amp;"', '"<": "&lt;"', '">": "&gt;"', '\'"\': "&quot;"'):
            with self.subTest(escaped=escaped):
                self.assertIn(escaped, HTML)

    def test_freshness_and_gap_tables_scroll_at_narrow_width(self):
        self.assertIn(".freshness-scroll { max-width: 100%; min-width: 0; overflow-x: auto;", HTML_NORMALIZED)
        self.assertIn(".freshness-table { width: max-content; min-width: 100%;", HTML_NORMALIZED)
        self.assertIn('<div class="freshness-scroll"> <table class="freshness-table">', HTML_NORMALIZED)
        self.assertEqual(HTML.count('<div class="freshness-scroll">'), 2)
        self.assertIn("Last observed (UTC)", HTML)
        self.assertIn("Gap start (UTC)", HTML)

    def test_panel_does_not_emit_local_time_or_outage_state(self):
        start = HTML.index('<section id="freshnessPanel"')
        panel = HTML[start:HTML.index("</section>", start)]
        self.assertIn("(UTC)", panel)
        self.assertNotIn("OFFLINE", panel)
        self.assertNotIn("toLocaleString", self.renderer)
        self.assertRegex(self.renderer, re.compile(r"utcValue\(gap\.gap_start\)"))
