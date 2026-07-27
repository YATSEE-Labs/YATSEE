"""
Tests for deterministic civic event generation.

Synthetic records artifacts keep the workflow fast and verify that no model,
embedding, or provider dependency is required.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from yatsee.cli.main import build_parser
from yatsee.intel.events import (
    build_events_artifact,
    render_events_markdown,
    run_events_stage,
)


class EventsTests(unittest.TestCase):
    """
    Validate conservative duplicate handling and decision grouping.
    """

    def _artifact(self, records: list[dict]) -> dict:
        """
        Build a minimal complete records artifact.

        :param records: Source record list
        :return: Complete records artifact
        """
        return {
            "artifact_kind": "records",
            "schema_version": "civic_records_v1",
            "status": "complete",
            "source_transcript": (
                "./data/test_entity/normalized/"
                "abc123def45.2026-6-18_test_meeting.txt"
            ),
            "source_sha256": "source-hash",
            "meeting_type": "county_board",
            "job_profile": "civic",
            "model": "gemma4:e4b",
            "record_count": len(records),
            "records": records,
        }

    def _record(
        self,
        record_type: str,
        subject: str,
        summary: str,
        evidence: str,
        chunk: int,
        **extra: object,
    ) -> dict:
        """
        Build one representative records-schema item.

        :param record_type: Record type
        :param subject: Record subject
        :param summary: Record summary
        :param evidence: Record evidence
        :param chunk: Source chunk identifier
        :param extra: Optional additional record fields
        :return: Source record
        """
        record = {
            "record_type": record_type,
            "subject": subject,
            "summary": summary,
            "evidence": evidence,
            "source_chunks": [chunk],
        }
        record.update(extra)
        return record

    def test_groups_motion_second_and_vote(self) -> None:
        """
        A local motion, standalone second, and vote should form one decision.
        """
        artifact = self._artifact(
            [
                self._record(
                    "motion",
                    "Approval of IGA",
                    "Motion to approve the IGA.",
                    "Mr. Adams moved to approve the IGA.",
                    1,
                    attribution="Mr. Adams",
                ),
                self._record(
                    "motion",
                    "Approval of IGA",
                    "Second for approval of the IGA.",
                    "Second by Mrs. Baker.",
                    1,
                    attribution="Mrs. Baker",
                ),
                self._record(
                    "vote",
                    "Approval of IGA",
                    "The IGA passed 12-0.",
                    "The motion passed 12-0.",
                    1,
                    attributes={"vote_outcome": "passed"},
                ),
            ]
        )

        events = build_events_artifact(artifact, meeting="test_meeting")

        self.assertEqual(events["event_count"], 1)
        event = events["events"][0]
        self.assertEqual(event["event_type"], "decision")
        self.assertEqual(event["source_record_indices"], [1, 2, 3])
        self.assertEqual(event["components"]["motion"], [1])
        self.assertEqual(event["components"]["second"], [2])
        self.assertEqual(event["components"]["vote"], [3])
        self.assertEqual(event["outcome"], "passed")
        self.assertEqual(event["attributions"], ["Mr. Adams", "Mrs. Baker"])

    def test_compatible_success_outcomes_do_not_conflict(self) -> None:
        """
        Approved and passed should be treated as compatible success terms.
        """
        artifact = self._artifact(
            [
                self._record(
                    "motion",
                    "Approval of Meeting Minutes",
                    "Motion to approve the meeting minutes.",
                    "Can I get a motion? Motion approved.",
                    1,
                    timestamp=100.0,
                ),
                self._record(
                    "vote",
                    "Approval of Meeting Minutes",
                    "The motion passed.",
                    "All those in favor say aye. Motion passes.",
                    1,
                    timestamp=50.0,
                ),
            ]
        )

        events = build_events_artifact(artifact, meeting="test_meeting")

        event = events["events"][0]
        self.assertEqual(event["outcome"], "passed")
        self.assertEqual(event["outcome_values"], ["approved", "passed"])
        self.assertEqual(event["timestamp"], 100.0)
        self.assertEqual(event["end_timestamp"], 100.0)
        self.assertNotIn("conflicting_outcomes", event.get("review_flags", []))

    def test_incompatible_outcomes_remain_conflicting(self) -> None:
        """
        A passed vote and failed motion remain an explicit contradiction.
        """
        artifact = self._artifact(
            [
                self._record(
                    "motion",
                    "Approval of Meeting Minutes",
                    "The motion failed.",
                    "The motion failed.",
                    1,
                ),
                self._record(
                    "vote",
                    "Approval of Meeting Minutes",
                    "The motion passed.",
                    "The motion passed.",
                    1,
                ),
            ]
        )

        events = build_events_artifact(artifact, meeting="test_meeting")

        event = events["events"][0]
        self.assertEqual(event["outcome"], "conflicting")
        self.assertEqual(event["outcome_values"], ["failed", "passed"])
        self.assertIn("conflicting_outcomes", event["review_flags"])

    def test_motion_to_approve_is_not_an_outcome(self) -> None:
        """
        Infinitive motion language must not be treated as an approved outcome.
        """
        artifact = self._artifact(
            [
                self._record(
                    "motion",
                    "Approval of claims",
                    "Motion to approve the claims.",
                    "I move to approve the claims.",
                    1,
                )
            ]
        )

        events = build_events_artifact(artifact, meeting="test_meeting")

        self.assertNotIn("outcome", events["events"][0])

    def test_collapses_identical_evidence_for_related_same_type(self) -> None:
        """
        Same-type adjacent chunk duplicates should collapse losslessly.
        """
        evidence = "Housing availability remains an issue throughout the county."
        artifact = self._artifact(
            [
                self._record(
                    "discussion",
                    "Housing availability issues",
                    "Housing availability remains a county-wide issue.",
                    evidence,
                    3,
                ),
                self._record(
                    "discussion",
                    "Housing availability and development ideas",
                    "The body discussed housing availability.",
                    evidence,
                    4,
                ),
            ]
        )

        events = build_events_artifact(artifact, meeting="test_meeting")

        self.assertEqual(events["event_count"], 1)
        self.assertEqual(events["duplicate_record_count"], 1)
        event = events["events"][0]
        self.assertEqual(event["source_record_indices"], [1, 2])
        self.assertEqual(
            event["grouping"]["duplicate_rules"],
            ["identical_evidence"],
        )
        self.assertEqual(event["evidence"][0]["source_record_indices"], [1, 2])

    def test_duplicate_vote_can_absorb_intervening_related_motion(self) -> None:
        """
        A cross-chunk duplicate vote should not strand an intervening motion.
        """
        vote_evidence = "The motion passed 13 to 0."
        artifact = self._artifact(
            [
                self._record(
                    "vote",
                    "Juneteenth recognition",
                    "The vote passed 13 to 0.",
                    vote_evidence,
                    2,
                ),
                self._record(
                    "motion",
                    "Juneteenth recognition resolution",
                    "Motion to approve Juneteenth recognition.",
                    "Mr. Bush moved to approve the resolution.",
                    3,
                ),
                self._record(
                    "vote",
                    "Juneteenth recognition resolution",
                    "The resolution passed 13 to 0.",
                    vote_evidence,
                    3,
                ),
            ]
        )

        events = build_events_artifact(artifact, meeting="test_meeting")

        self.assertEqual(events["event_count"], 1)
        self.assertEqual(events["events"][0]["source_record_indices"], [1, 2, 3])
        self.assertEqual(events["events"][0]["outcome"], "passed")

    def test_does_not_collapse_different_record_types(self) -> None:
        """
        Identical evidence may support multiple facts with different types.
        """
        evidence = "The team completed two million steps."
        artifact = self._artifact(
            [
                self._record(
                    "metric",
                    "Team steps total",
                    "The team completed two million steps.",
                    evidence,
                    4,
                ),
                self._record(
                    "recognition",
                    "Team achievement award",
                    "The team received an award.",
                    evidence,
                    5,
                ),
            ]
        )

        events = build_events_artifact(artifact, meeting="test_meeting")

        self.assertEqual(events["event_count"], 2)
        self.assertEqual(events["duplicate_record_count"], 0)

    def test_unattached_second_is_preserved_and_flagged(self) -> None:
        """
        A second with no nearby motion or vote must remain visible for review.
        """
        artifact = self._artifact(
            [
                self._record(
                    "procedural_action",
                    "Claims approval second",
                    "The motion was seconded.",
                    "Second by Mr. Newton.",
                    1,
                ),
                self._record(
                    "discussion",
                    "Road work",
                    "The committee discussed road work.",
                    "Road work begins next month.",
                    1,
                ),
            ]
        )

        events = build_events_artifact(artifact, meeting="test_meeting")

        first = events["events"][0]
        self.assertEqual(first["review_flags"], ["unattached_second"])
        self.assertEqual(events["review_flag_counts"]["unattached_second"], 1)

    def test_extracts_meeting_date_from_source_path(self) -> None:
        """
        Entity indexes should receive an ISO date from stable artifact metadata.
        """
        artifact = self._artifact(
            [
                self._record(
                    "discussion",
                    "Road work",
                    "The body discussed road work.",
                    "Road work begins next month.",
                    1,
                )
            ]
        )

        events = build_events_artifact(artifact, meeting="abc123def45.test")

        self.assertEqual(events["meeting_date"], "2026-06-18")

    def test_event_timestamp_rejects_single_early_outlier(self) -> None:
        """
        A duplicate alignment outlier must not pull the event link backward.
        """
        artifact = self._artifact(
            [
                self._record(
                    "motion",
                    "Approval of zoning amendment",
                    "Motion to approve the zoning amendment.",
                    "Is there a motion to approve?",
                    1,
                    timestamp=383.0,
                ),
                self._record(
                    "motion",
                    "Approval of zoning amendment",
                    "Motion to approve the zoning amendment.",
                    "Motion by Mr. Adams.",
                    2,
                    timestamp=681.0,
                ),
                self._record(
                    "vote",
                    "Approval of zoning amendment",
                    "The amendment passed.",
                    "The motion passed.",
                    2,
                    timestamp=681.0,
                ),
            ]
        )

        events = build_events_artifact(artifact, meeting="test_meeting")

        self.assertEqual(events["event_count"], 1)
        self.assertEqual(events["events"][0]["timestamp"], 681.0)

    def test_presentation_history_does_not_become_event_outcome(self) -> None:
        """
        Historical approval language in a presentation is not an event outcome.
        """
        artifact = self._artifact(
            [
                self._record(
                    "presentation",
                    "Zoning amendment history",
                    "The incorrect version was approved last month.",
                    "The director explained that the prior version was approved.",
                    1,
                )
            ]
        )

        events = build_events_artifact(artifact, meeting="test_meeting")

        self.assertNotIn("outcome", events["events"][0])

    def test_markdown_renders_reviewable_provenance(self) -> None:
        """
        Markdown should expose reviewable provenance without internal record IDs.
        """
        artifact = self._artifact(
            [
                self._record(
                    "vote",
                    "Approval of agenda",
                    "The agenda passed.",
                    "The motion passed.",
                    1,
                    timestamp=65.0,
                    video_id="abc123def45",
                )
            ]
        )
        events = build_events_artifact(artifact, meeting="test_meeting")

        markdown = render_events_markdown(events)

        self.assertIn("# Meeting Events", markdown)
        self.assertIn("Approval of agenda", markdown)
        self.assertIn("Passed", markdown)
        self.assertIn("**Summary:** The agenda passed.", markdown)
        evidence_marker = "**Evidence:** “The motion passed.”"
        timestamp_marker = "**Source timestamp:** [01:05]"
        self.assertIn(evidence_marker, markdown)
        self.assertIn("watch?v=abc123def45&t=65s", markdown)
        self.assertIn("**Source timestamp:**", markdown)
        self.assertLess(
            markdown.index(evidence_marker),
            markdown.index(timestamp_marker),
        )
        self.assertNotIn("Source records:", markdown)
        self.assertNotIn("Source chunks:", markdown)
        self.assertEqual(events["events"][0]["source_record_indices"], [1])
        self.assertEqual(events["events"][0]["source_chunks"], [1])

    def test_markdown_escapes_untrusted_event_text(self) -> None:
        """
        Generated fields should render as text rather than active Markdown or HTML.
        """
        artifact = self._artifact(
            [
                self._record(
                    "discussion",
                    "Budget [link](https://example.invalid)",
                    "Use *literal* formatting.",
                    "<script>alert(1)</script>\nNext line",
                    1,
                )
            ]
        )

        markdown = render_events_markdown(
            build_events_artifact(artifact, meeting="test_meeting")
        )

        self.assertIn(r"Budget \[link\](https://example.invalid)", markdown)
        self.assertIn(r"Use \*literal\* formatting.", markdown)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; Next line", markdown)
        self.assertNotIn("<script>", markdown)

    def test_run_stage_writes_meeting_and_entity_indexes(self) -> None:
        """
        The entity workflow should write event artifacts and aggregate indexes.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            entity_dir = root / "data" / "test_entity"
            meeting = "abc123def45.2026-6-18_test_meeting"
            meeting_dir = entity_dir / "intel_gemma4_e4b" / "records" / meeting
            meeting_dir.mkdir(parents=True)
            (entity_dir / "config.toml").write_text(
                """
[settings]
data_path = "./data/test_entity"
records_model = "gemma4:e4b"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            config_path = root / "yatsee.toml"
            config_path.write_text(
                """
[system]
root_data_dir = "./data"
default_records_model = "gemma4:e4b"

[models."gemma4:e4b"]
records_max_tokens = 1500
num_ctx = 16384

[entities.test_entity]
display_name = "Test Entity"
entity = "test_entity"
base = ""
""".strip()
                + "\n",
                encoding="utf-8",
            )
            records = self._artifact(
                [
                    self._record(
                        "motion",
                        "Approval of agenda",
                        "Motion to approve the agenda.",
                        "Mr. Adams moved to approve the agenda.",
                        1,
                    ),
                    self._record(
                        "vote",
                        "Approval of agenda",
                        "The agenda passed.",
                        "The motion passed.",
                        1,
                    ),
                ]
            )
            records_path = meeting_dir / f"{meeting}.records.json"
            records_path.write_text(
                json.dumps(records, indent=2) + "\n",
                encoding="utf-8",
            )

            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                result = run_events_stage(
                    str(config_path),
                    entity="test_entity",
                    model="gemma4:e4b",
                )
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["event_count"], 1)
            output_root = entity_dir / "intel_gemma4_e4b" / "events"
            self.assertTrue((output_root / "events.jsonl").is_file())
            self.assertTrue((output_root / "meetings.json").is_file())
            self.assertTrue(
                (output_root / meeting / f"{meeting}.events.json").is_file()
            )
            index = json.loads(
                (output_root / "meetings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(index["meeting_count"], 1)
            self.assertEqual(index["event_count"], 1)

    def test_cli_registers_events_command(self) -> None:
        """
        The root parser should expose the deterministic events workflow.
        """
        parser = build_parser()
        args = parser.parse_args(
            [
                "intel",
                "events",
                "-e",
                "test_entity",
                "--model",
                "gemma4:e4b",
                "--meeting",
                "2026-6-18",
            ]
        )

        self.assertEqual(args.intel_command, "events")
        self.assertEqual(args.entity, "test_entity")
        self.assertEqual(args.model, "gemma4:e4b")
        self.assertEqual(args.meeting_filter, "2026-6-18")


if __name__ == "__main__":
    unittest.main()
