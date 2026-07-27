"""
Tests for deterministic records corpus auditing.

These tests use synthetic artifacts so audit behavior remains fast and does not
require optional model, embedding, or provider dependencies.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yatsee.cli.main import build_parser
from yatsee.core.errors import ValidationError
from yatsee.audit.records import run_records_audit


class RecordsAuditTests(unittest.TestCase):
    """
    Validate corpus discovery, risk detection, and report writing.
    """

    def _write_config(self, root: Path) -> tuple[Path, Path]:
        """
        Create a minimal entity and model configuration for an audit run.

        :param root: Temporary project root
        :return: Global config path and model-specific records directory
        """
        entity = "test_entity"
        entity_dir = root / "data" / entity
        records_dir = entity_dir / "intel_gemma4_e4b" / "records"
        records_dir.mkdir(parents=True)
        (entity_dir / "config.toml").write_text(
            """
[settings]
data_path = "./data/test_entity"
records_model = "gemma4:e4b"

[titles]
officials = ["Mayor", "Alderman"]

[people.council]
Mayor_Miller = ["Mayor Miller", "Miller"]
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
        return config_path, records_dir

    def _write_transcript(self, root: Path, meeting: str) -> Path:
        """
        Write one normalized transcript used by rebuild planning tests.

        :param root: Temporary project root
        :param meeting: Transcript basename without extension
        :return: Normalized transcript path
        """
        normalized_dir = root / "data" / "test_entity" / "normalized"
        normalized_dir.mkdir(parents=True, exist_ok=True)
        path = normalized_dir / f"{meeting}.txt"
        path.write_text("The body met and considered public business.\n", encoding="utf-8")
        return path

    def _write_complete_artifact(self, meeting_dir: Path) -> None:
        """
        Write one complete artifact containing representative audit defects.

        :param meeting_dir: Meeting output directory
        :return: None
        """
        meeting_dir.mkdir(parents=True, exist_ok=True)
        records = [
            {
                "record_type": "public_comment",
                "subject": "Mayor Miller comments on funding",
                "summary": "Mayor Miller discussed funding.",
                "evidence": "Mayor Miller said the funding should continue.",
                "attribution": "Mayor Miller",
                "source_chunks": [1],
            },
            {
                "record_type": "motion",
                "subject": "Second to approve agenda",
                "summary": "A second was offered.",
                "evidence": "Second.",
                "attributes": {"seconded_by": "Unknown"},
                "source_chunks": [1],
            },
            {
                "record_type": "financial",
                "subject": "Grant amount",
                "summary": "The grant amount is $500.",
                "evidence": "The grant amount is $400.",
                "source_chunks": [2],
            },
            {
                "record_type": "discussion",
                "subject": "Grant sunset date",
                "summary": "The grant expires at the end of June.",
                "evidence": "The grant sunsets at the end of June.",
                "source_chunks": [2],
            },
            {
                "record_type": "follow_up",
                "subject": "Grant expiration date",
                "summary": "The grant ends at the end of June.",
                "evidence": "The grant expires at the end of June.",
                "source_chunks": [3],
            },
            {
                "record_type": "vote",
                "subject": "Vote on grant",
                "summary": "The body voted on the grant.",
                "evidence": "They voted on the grant.",
                "source_chunks": [3],
            },
        ]
        artifact = {
            "artifact_kind": "records",
            "schema_version": "civic_records_v1",
            "status": "complete",
            "meeting_type": "city_council",
            "chunk_count": 3,
            "successful_chunk_count": 3,
            "partial_chunk_count": 0,
            "failed_chunk_count": 0,
            "validation_warning_count": 0,
            "normalized_record_count": 0,
            "rejected_record_count": 0,
            "rejected_line_count": 0,
            "record_count": len(records),
            "records": records,
            "timestamp_alignment": {
                "match_rate": 0.89,
                "matched_record_count": 3,
                "ambiguous_record_count": 0,
                "unmatched_record_count": 3,
            },
        }
        base_name = meeting_dir.name
        (meeting_dir / f"{base_name}.records.json").write_text(
            json.dumps(artifact, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_partial_artifact(self, meeting_dir: Path) -> None:
        """
        Write one interrupted meeting artifact for hard-failure ranking.

        :param meeting_dir: Meeting output directory
        :return: None
        """
        meeting_dir.mkdir(parents=True, exist_ok=True)
        artifact = {
            "artifact_kind": "records",
            "schema_version": "civic_records_v1",
            "status": "partial",
            "meeting_type": "city_council",
            "chunk_count": 4,
            "successful_chunk_count": 2,
            "partial_chunk_count": 1,
            "failed_chunk_count": 1,
            "record_count": 0,
            "records": [],
        }
        base_name = meeting_dir.name
        (meeting_dir / f"{base_name}.records.partial.json").write_text(
            json.dumps(artifact, indent=2) + "\n",
            encoding="utf-8",
        )


    def _write_tuned_heuristic_artifact(self, meeting_dir: Path) -> None:
        """
        Write one artifact exercising tuned audit precision and recall.

        :param meeting_dir: Meeting output directory
        :return: None
        """
        meeting_dir.mkdir(parents=True, exist_ok=True)
        records = [
            {
                "record_type": "vote",
                "subject": "Approve the agenda",
                "summary": "The agenda passed 12-0.",
                "evidence": "And that passes 12-0.",
                "source_chunks": [1],
            },
            {
                "record_type": "vote",
                "subject": "Approve the consent agenda",
                "summary": "The consent agenda passed 12-0.",
                "evidence": "And that passes 12-0.",
                "source_chunks": [1],
            },
            {
                "record_type": "procedural_action",
                "subject": "Meeting start",
                "summary": "The meeting started at 6:05 PM.",
                "evidence": "It is 6.05, and we are going to start.",
                "source_chunks": [1],
            },
            {
                "record_type": "metric",
                "subject": "Enterprise Zone abatement",
                "summary": "The abatement is 100% for three years.",
                "evidence": "Three years at a hundred percent.",
                "source_chunks": [1],
            },
            {
                "record_type": "financial",
                "subject": "Moderator election",
                "summary": "The moderator will be paid $100.",
                "evidence": "The electors voted for the moderator.",
                "source_chunks": [1],
            },
            {
                "record_type": "financial",
                "subject": "Budget comparison",
                "summary": "The budget was $1.5 million and spending was $1.1 million.",
                "evidence": "We budgeted $1.5 million and are coming in at $1.1.",
                "source_chunks": [1],
            },
            {
                "record_type": "financial",
                "subject": "Fund balance surplus",
                "summary": "The operation produced a surplus just shy of $80,000.",
                "evidence": "You operate in a surplus of just shy of 80,000.",
                "source_chunks": [1],
            },
            {
                "record_type": "metric",
                "subject": "General fund deposits",
                "summary": "Total deposits to date are $151,345.77.",
                "evidence": "The total deposits to date are 151,345.77.",
                "source_chunks": [1],
            },
            {
                "record_type": "financial",
                "subject": "Short-term investments",
                "summary": "The county placed an additional $2.7 million.",
                "evidence": "I placed another 2.7 million in short-term investments.",
                "source_chunks": [1],
            },
            {
                "record_type": "metric",
                "subject": "Monthly fund comparison",
                "summary": "Total county funds were down 1.68%.",
                "evidence": "Total county funds were down 1.68 percent.",
                "source_chunks": [1],
            },
            {
                "record_type": "discussion",
                "subject": "Appropriation adjustment",
                "summary": "The ending balance increased by $999.",
                "evidence": "Everything went up 999 dollars.",
                "source_chunks": [1],
            },
            {
                "record_type": "discussion",
                "subject": "Clerk line-item adjustment",
                "summary": "The clerk line item was reduced by $908.",
                "evidence": "It was 908 less than last year.",
                "source_chunks": [1],
            },
            {
                "record_type": "public_comment",
                "subject": "Mayor discusses zoning",
                "summary": "A resident discussed zoning.",
                "evidence": "I am concerned about the zoning proposal.",
                "source_chunks": [2],
            },
            {
                "record_type": "public_comment",
                "subject": "Funding comments",
                "summary": "Mayor Miller discussed funding.",
                "evidence": "The funding should continue.",
                "attribution": "Mayor Miller",
                "source_chunks": [2],
            },
            {
                "record_type": "motion",
                "subject": "Approve the contract",
                "summary": "Mr. Smith moved to approve the contract.",
                "evidence": "Is there a motion to approve by Mr. Smith?",
                "attributes": {"made_by": "Mr. Smith"},
                "source_chunks": [2],
            },
            {
                "record_type": "motion",
                "subject": "Approve the policy",
                "summary": "A motion was requested to approve the policy.",
                "evidence": "Is there a motion to approve?",
                "source_chunks": [2],
            },
            {
                "record_type": "discussion",
                "subject": "Policy seconding",
                "summary": "Mr. Moderow seconded the policy motion.",
                "evidence": "By Mr. Moderow.",
                "source_chunks": [2],
            },
            {
                "record_type": "discussion",
                "subject": "Truck type comparison",
                "summary": "Members compared the two truck types.",
                "evidence": "Are these both the same type of trucks or different trucks?",
                "source_chunks": [2],
            },
            {
                "record_type": "discussion",
                "subject": "Comparison of purchased trucks",
                "summary": "Members asked whether the purchased trucks were the same type.",
                "evidence": "Are these both the same type of trucks or different trucks?",
                "source_chunks": [3],
            },
        ]
        artifact = {
            "artifact_kind": "records",
            "schema_version": "civic_records_v1",
            "status": "complete",
            "meeting_type": "city_council",
            "chunk_count": 3,
            "successful_chunk_count": 3,
            "partial_chunk_count": 0,
            "failed_chunk_count": 0,
            "validation_warning_count": 0,
            "normalized_record_count": 0,
            "rejected_record_count": 0,
            "rejected_line_count": 0,
            "record_count": len(records),
            "records": records,
            "timestamp_alignment": {
                "match_rate": 1.0,
                "matched_record_count": len(records),
                "ambiguous_record_count": 0,
                "unmatched_record_count": 0,
            },
        }
        base_name = meeting_dir.name
        (meeting_dir / f"{base_name}.records.json").write_text(
            json.dumps(artifact, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_profile_artifact(self, meeting_dir: Path) -> None:
        """
        Write one artifact whose findings differ by audit sensitivity profile.

        :param meeting_dir: Meeting output directory
        :return: None
        """
        meeting_dir.mkdir(parents=True, exist_ok=True)
        records = [
            {
                "record_type": "discussion",
                "subject": "Deployment options",
                "summary": (
                    "The committee compared three deployment options and selected "
                    "a preferred approach after reviewing cost, staffing, security, "
                    "maintenance, implementation, support, training, and operational "
                    "requirements for the full project."
                ),
                "evidence": "Options were discussed.",
                "attribution": "Unknown",
                "source_chunks": [1],
            }
        ]
        artifact = {
            "artifact_kind": "records",
            "schema_version": "civic_records_v1",
            "status": "complete",
            "meeting_type": "city_council",
            "chunk_count": 1,
            "successful_chunk_count": 1,
            "partial_chunk_count": 0,
            "failed_chunk_count": 0,
            "validation_warning_count": 0,
            "normalized_record_count": 0,
            "rejected_record_count": 0,
            "rejected_line_count": 0,
            "record_count": len(records),
            "records": records,
            "timestamp_alignment": {
                "match_rate": 0.96,
                "matched_record_count": 1,
                "ambiguous_record_count": 0,
                "unmatched_record_count": 0,
            },
        }
        base_name = meeting_dir.name
        (meeting_dir / f"{base_name}.records.json").write_text(
            json.dumps(artifact, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_audit_ranks_failures_and_writes_all_report_formats(self) -> None:
        """
        Audit should rank hard failures and preserve exact suspect-record details.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path, records_dir = self._write_config(root)
            self._write_complete_artifact(
                records_dir / "abc.committee_of_the_whole_1-1-24"
            )
            self._write_partial_artifact(records_dir / "meeting_partial")

            previous_cwd = Path.cwd()
            try:
                # Entity-local data_path is intentionally project-relative.
                import os

                os.chdir(root)
                result = run_records_audit(
                    global_config_path=str(config_path),
                    entity="test_entity",
                    model="gemma4:e4b",
                )
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(result["meeting_count"], 2)
            report = result["report"]
            self.assertEqual(report["meetings"][0]["meeting"], "meeting_partial")

            complete = next(
                meeting
                for meeting in report["meetings"]
                if meeting["meeting"] == "abc.committee_of_the_whole_1-1-24"
            )
            finding_codes = {finding["code"] for finding in complete["findings"]}
            self.assertIn("meeting_type_filename_mismatch", finding_codes)
            self.assertIn("official_public_comment_candidate", finding_codes)
            self.assertIn("standalone_seconder_motion", finding_codes)
            self.assertIn("unknown_optional_value", finding_codes)
            self.assertIn("summary_evidence_quantity_mismatch", finding_codes)
            self.assertIn("adjacent_chunk_duplicate_candidate", finding_codes)
            self.assertIn("vote_without_outcome_evidence", finding_codes)
            self.assertIn("low_timestamp_alignment_rate", finding_codes)
            self.assertIn("unmatched_timestamp_records", finding_codes)

            for report_path in result["report_paths"].values():
                self.assertTrue(Path(report_path).is_file())

            previous_cwd = Path.cwd()
            try:
                import os

                os.chdir(root)
                rerun = run_records_audit(
                    global_config_path=str(config_path),
                    entity="test_entity",
                    model="gemma4:e4b",
                )
            finally:
                os.chdir(previous_cwd)
            self.assertEqual(rerun["meeting_count"], 2)

            markdown = Path(result["report_paths"]["markdown"]).read_text(
                encoding="utf-8"
            )
            self.assertIn("# Records Audit", markdown)
            self.assertIn("meeting_partial", markdown)
            self.assertIn("summary_evidence_quantity_mismatch", markdown)


    def test_tuned_audit_reduces_boilerplate_false_positives(self) -> None:
        """
        Audit should normalize quantities and require substantive duplicate identity.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path, records_dir = self._write_config(root)
            self._write_tuned_heuristic_artifact(records_dir / "precision_meeting")

            previous_cwd = Path.cwd()
            try:
                import os

                os.chdir(root)
                result = run_records_audit(
                    global_config_path=str(config_path),
                    entity="test_entity",
                )
            finally:
                os.chdir(previous_cwd)

            meeting = result["report"]["meetings"][0]
            findings = meeting["findings"]
            finding_codes = [finding["code"] for finding in findings]

            self.assertNotIn("vote_without_outcome_evidence", finding_codes)
            quantity_findings = [
                finding
                for finding in findings
                if finding["code"] == "summary_evidence_quantity_mismatch"
            ]
            self.assertEqual(len(quantity_findings), 1)
            self.assertEqual(quantity_findings[0]["severity"], "high")
            self.assertIn("currency:100", quantity_findings[0]["details"]["summary_only_quantities"])

            official_findings = [
                finding
                for finding in findings
                if finding["code"] == "official_public_comment_candidate"
            ]
            self.assertEqual(len(official_findings), 1)
            self.assertEqual(official_findings[0]["record_indices"], [14])

            motion_requests = [
                finding
                for finding in findings
                if finding["code"] == "motion_request_not_motion"
            ]
            self.assertEqual(len(motion_requests), 1)
            self.assertEqual(motion_requests[0]["record_indices"], [16])

            standalone_seconds = [
                finding
                for finding in findings
                if finding["code"] == "standalone_seconder_motion"
            ]
            self.assertEqual(len(standalone_seconds), 1)
            self.assertEqual(standalone_seconds[0]["record_indices"], [17])

            duplicate_findings = [
                finding
                for finding in findings
                if finding["code"] == "adjacent_chunk_duplicate_candidate"
            ]
            self.assertEqual(len(duplicate_findings), 1)
            self.assertEqual(duplicate_findings[0]["record_indices"], [18, 19])

    def test_balanced_and_strict_profiles_apply_distinct_expectations(self) -> None:
        """
        Strict mode should add higher-tier semantic checks without changing defaults.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path, records_dir = self._write_config(root)
            self._write_profile_artifact(
                records_dir / "sample.committee_of_the_whole_meeting"
            )

            previous_cwd = Path.cwd()
            try:
                import os

                os.chdir(root)
                balanced = run_records_audit(
                    global_config_path=str(config_path),
                    entity="test_entity",
                )
                strict = run_records_audit(
                    global_config_path=str(config_path),
                    entity="test_entity",
                    audit_profile="strict",
                )
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(balanced["audit_profile"], "balanced")
            self.assertEqual(strict["audit_profile"], "strict")
            self.assertEqual(balanced["report"]["schema_version"], "records_audit_v2")
            self.assertEqual(balanced["report"]["audit_profile"], "balanced")
            self.assertEqual(strict["report"]["audit_profile"], "strict")

            balanced_findings = balanced["report"]["meetings"][0]["findings"]
            strict_findings = strict["report"]["meetings"][0]["findings"]
            balanced_codes = [finding["code"] for finding in balanced_findings]
            strict_codes = [finding["code"] for finding in strict_findings]

            self.assertNotIn("summary_evidence_quantity_mismatch", balanced_codes)
            self.assertIn("summary_evidence_quantity_mismatch", strict_codes)
            self.assertNotIn("summary_evidence_detail_gap", balanced_codes)
            self.assertIn("summary_evidence_detail_gap", strict_codes)
            self.assertNotIn("weak_timestamp_alignment_rate", balanced_codes)
            self.assertIn("weak_timestamp_alignment_rate", strict_codes)

            balanced_unknown = next(
                finding
                for finding in balanced_findings
                if finding["code"] == "unknown_optional_value"
            )
            strict_unknown = next(
                finding
                for finding in strict_findings
                if finding["code"] == "unknown_optional_value"
            )
            self.assertEqual(balanced_unknown["severity"], "low")
            self.assertEqual(strict_unknown["severity"], "medium")

            balanced_routing = next(
                finding
                for finding in balanced_findings
                if finding["code"] == "meeting_type_filename_mismatch"
            )
            strict_routing = next(
                finding
                for finding in strict_findings
                if finding["code"] == "meeting_type_filename_mismatch"
            )
            self.assertEqual(balanced_routing["severity"], "medium")
            self.assertEqual(strict_routing["severity"], "high")

            self.assertTrue(
                balanced["report_paths"]["json"].endswith("report.json")
            )
            self.assertTrue(
                strict["report_paths"]["json"].endswith("report.strict.json")
            )
            self.assertTrue(Path(balanced["report_paths"]["json"]).is_file())
            self.assertTrue(Path(strict["report_paths"]["json"]).is_file())

            markdown = Path(strict["report_paths"]["markdown"]).read_text(
                encoding="utf-8"
            )
            csv_text = Path(strict["report_paths"]["csv"]).read_text(
                encoding="utf-8"
            )
            self.assertIn("Audit profile: `strict`", markdown)
            self.assertTrue(csv_text.startswith("audit_profile,"))

    def test_strict_cli_alias_selects_the_strict_profile(self) -> None:
        """
        The convenience flag should resolve to the same explicit strict profile.
        """
        parser = build_parser()
        args = parser.parse_args(
            ["audit", "records", "-e", "test_entity", "--strict"]
        )
        self.assertEqual(args.audit_profile, "strict")

        args = parser.parse_args(
            ["audit", "records", "-e", "test_entity", "--profile", "balanced"]
        )
        self.assertEqual(args.audit_profile, "balanced")

        args = parser.parse_args(
            ["audit", "records", "-e", "test_entity", "--rebuild"]
        )
        self.assertEqual(args.rebuild_mode, "plan")

        args = parser.parse_args(
            ["audit", "records", "-e", "test_entity", "--rebuild-auto"]
        )
        self.assertEqual(args.rebuild_mode, "auto")

        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "audit",
                    "records",
                    "-e",
                    "test_entity",
                    "--rebuild",
                    "--rebuild-auto",
                ]
            )

    def test_invalid_audit_profile_is_rejected(self) -> None:
        """
        Programmatic callers should receive a clear validation error for bad profiles.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path, _ = self._write_config(root)
            with self.assertRaises(ValidationError):
                run_records_audit(
                    global_config_path=str(config_path),
                    entity="test_entity",
                    audit_profile="aggressive",
                )

    def test_meeting_filter_limits_the_audit_scope(self) -> None:
        """
        A meeting substring should narrow discovery without changing source artifacts.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path, records_dir = self._write_config(root)
            self._write_complete_artifact(records_dir / "alpha_meeting")
            self._write_complete_artifact(records_dir / "beta_meeting")

            previous_cwd = Path.cwd()
            try:
                import os

                os.chdir(root)
                result = run_records_audit(
                    global_config_path=str(config_path),
                    entity="test_entity",
                    meeting_filter="beta",
                    output_format="json",
                )
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(result["meeting_count"], 1)
            self.assertEqual(result["report"]["meetings"][0]["meeting"], "beta_meeting")
            self.assertEqual(set(result["report_paths"]), {"json"})
            expected_output = (
                root
                / "data"
                / "test_entity"
                / "audit"
                / "records"
                / "gemma4_e4b"
                / "meetings"
                / "beta"
            )
            self.assertEqual(Path(result["output_dir"]), expected_output)
            self.assertTrue((expected_output / "report.json").is_file())
            self.assertFalse(
                (expected_output.parents[1] / "report.json").exists()
            )


    def test_rebuild_plan_ignores_heuristics_and_selects_structural_failure(self) -> None:
        """
        Rebuild planning should ignore high heuristic findings and select partial output.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path, records_dir = self._write_config(root)
            self._write_complete_artifact(records_dir / "quality_meeting")
            self._write_partial_artifact(records_dir / "meeting_partial")
            self._write_transcript(root, "quality_meeting")
            self._write_transcript(root, "meeting_partial")

            previous_cwd = Path.cwd()
            try:
                import os

                os.chdir(root)
                result = run_records_audit(
                    global_config_path=str(config_path),
                    entity="test_entity",
                    rebuild_mode="plan",
                )
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(result["rebuild_candidate_count"], 1)
            candidate = result["rebuild_plan"]["candidates"][0]
            self.assertEqual(candidate["meeting"], "meeting_partial")
            self.assertTrue(candidate["rebuildable"])
            reason_codes = {reason["code"] for reason in candidate["reasons"]}
            self.assertIn("partial_records_artifact", reason_codes)
            self.assertIn("failed_chunks", reason_codes)
            self.assertNotIn("summary_evidence_quantity_mismatch", reason_codes)
            self.assertNotIn("standalone_seconder_motion", reason_codes)
            self.assertTrue(Path(result["rebuild_manifest_path"]).is_file())

    def test_rebuild_plan_can_start_from_missing_records_root(self) -> None:
        """
        Rebuild planning should list normalized transcripts even before records exist.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path, records_dir = self._write_config(root)
            shutil.rmtree(records_dir)
            self._write_transcript(root, "first_meeting")

            previous_cwd = Path.cwd()
            try:
                import os

                os.chdir(root)
                result = run_records_audit(
                    global_config_path=str(config_path),
                    entity="test_entity",
                    rebuild_mode="plan",
                )
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(result["meeting_count"], 0)
            self.assertEqual(result["rebuild_candidate_count"], 1)
            self.assertEqual(
                result["rebuild_plan"]["candidates"][0]["meeting"],
                "first_meeting",
            )

    def test_rebuild_plan_detects_transcript_without_artifact_directory(self) -> None:
        """
        Rebuild planning should compare normalized inputs with existing meeting outputs.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path, records_dir = self._write_config(root)
            self._write_complete_artifact(records_dir / "existing_meeting")
            self._write_transcript(root, "existing_meeting")
            self._write_transcript(root, "missing_meeting")

            previous_cwd = Path.cwd()
            try:
                import os

                os.chdir(root)
                result = run_records_audit(
                    global_config_path=str(config_path),
                    entity="test_entity",
                    rebuild_mode="plan",
                )
            finally:
                os.chdir(previous_cwd)

            candidates = {
                candidate["meeting"]: candidate
                for candidate in result["rebuild_plan"]["candidates"]
            }
            self.assertEqual(set(candidates), {"missing_meeting"})
            self.assertTrue(candidates["missing_meeting"]["rebuildable"])
            self.assertEqual(
                candidates["missing_meeting"]["reasons"][0]["code"],
                "missing_meeting_artifact_directory",
            )

    def test_rebuild_plan_requires_material_rejection_rate(self) -> None:
        """
        Isolated validation loss should not rebuild a meeting, but material loss should.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path, records_dir = self._write_config(root)
            self._write_complete_artifact(records_dir / "isolated_rejection")
            self._write_complete_artifact(records_dir / "material_rejection")
            self._write_transcript(root, "isolated_rejection")
            self._write_transcript(root, "material_rejection")

            isolated_path = (
                records_dir
                / "isolated_rejection"
                / "isolated_rejection.records.json"
            )
            isolated = json.loads(isolated_path.read_text(encoding="utf-8"))
            isolated["rejected_line_count"] = 1
            isolated_path.write_text(json.dumps(isolated), encoding="utf-8")

            material_path = (
                records_dir
                / "material_rejection"
                / "material_rejection.records.json"
            )
            material = json.loads(material_path.read_text(encoding="utf-8"))
            material["rejected_record_count"] = 3
            material_path.write_text(json.dumps(material), encoding="utf-8")

            previous_cwd = Path.cwd()
            try:
                import os

                os.chdir(root)
                result = run_records_audit(
                    global_config_path=str(config_path),
                    entity="test_entity",
                    rebuild_mode="plan",
                )
            finally:
                os.chdir(previous_cwd)

            candidates = {
                candidate["meeting"]: candidate
                for candidate in result["rebuild_plan"]["candidates"]
            }
            self.assertEqual(set(candidates), {"material_rejection"})
            self.assertEqual(
                candidates["material_rejection"]["reasons"][0]["code"],
                "excessive_validation_rejection",
            )

    def test_rebuild_auto_attempts_each_candidate_once_and_reaudits(self) -> None:
        """
        Automatic rebuilding should perform one forced attempt and stop after re-audit.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path, records_dir = self._write_config(root)
            meeting = "meeting_partial"
            meeting_dir = records_dir / meeting
            self._write_partial_artifact(meeting_dir)
            source_path = self._write_transcript(root, meeting)

            def fake_records_stage(**kwargs):
                self.assertEqual(Path(kwargs["input_path"]), source_path)
                self.assertTrue(kwargs["force"])
                self._write_complete_artifact(meeting_dir)
                return {
                    "results": [
                        {
                            "status": "complete",
                            "output_path": str(
                                meeting_dir / f"{meeting}.records.json"
                            ),
                            "record_count": 6,
                            "failed_chunk_count": 0,
                            "partial_chunk_count": 0,
                            "validation_warning_count": 0,
                        }
                    ]
                }

            previous_cwd = Path.cwd()
            try:
                import os

                os.chdir(root)
                with patch(
                    "yatsee.intel.record.run_records_stage",
                    side_effect=fake_records_stage,
                ) as mocked_stage:
                    result = run_records_audit(
                        global_config_path=str(config_path),
                        entity="test_entity",
                        rebuild_mode="auto",
                    )
            finally:
                os.chdir(previous_cwd)

            mocked_stage.assert_called_once()
            self.assertEqual(result["rebuild_execution_summary"]["attempted"], 1)
            self.assertEqual(result["rebuild_execution_summary"]["complete"], 1)
            self.assertEqual(result["remaining_rebuild_candidate_count"], 0)

    def test_configured_intel_path_cannot_escape_entity_data_path(self) -> None:
        """
        Audit path resolution should reject an unsafe model workspace.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path, _ = self._write_config(root)
            config_text = config_path.read_text(encoding="utf-8").replace(
                'records_max_tokens = 1500',
                'intel_append_dir = "../../outside"\nrecords_max_tokens = 1500',
            )
            config_path.write_text(config_text, encoding="utf-8")

            previous_cwd = Path.cwd()
            try:
                import os

                os.chdir(root)
                with self.assertRaises(ValidationError):
                    run_records_audit(
                        global_config_path=str(config_path),
                        entity="test_entity",
                    )
            finally:
                os.chdir(previous_cwd)



if __name__ == "__main__":
    unittest.main()
