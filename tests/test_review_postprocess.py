import tempfile
import unittest
from pathlib import Path

from doi_pipeline.review_postprocess import (
    PostprocessDecision,
    apply_decision,
    ecs_variants,
    postprocess_review_records,
    stage2_decide,
    stage3_decide,
)


class ReviewPostprocessTests(unittest.TestCase):
    def test_stage2_accepts_strong_existing_scores(self) -> None:
        record = {
            "status": "review",
            "doi": "10.1021/acsenergylett.4c01234",
            "title": "A Practical Battery Paper",
            "authors": "Ada Lovelace; Grace Hopper",
            "doi_validation_score": '{"title_score":0.89,"author_score":0.8}',
        }
        decision = stage2_decide(record)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.decision, "stage2_accept_probable")
        self.assertEqual(decision.confidence, "medium")

    def test_stage3_accepts_resolved_filename_doi_with_bad_local_title(self) -> None:
        record = {
            "status": "review",
            "filename": "10.1016_j.jpowsour.2020.228999.pdf",
            "doi": "10.1016/j.jpowsour.2020.228999",
            "doi_source": "filename_doi",
            "title": "Accepted Manuscript",
            "authors": "",
        }
        authority = {
            "resolved": True,
            "authority_doi": "10.1016/j.jpowsour.2020.228999",
            "authority_title": "A Different But Real Battery Article",
            "authority_authors": ["Ada Lovelace"],
            "error": "",
        }
        decision = stage3_decide(record, authority)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.decision, "stage3_accept_probable")

    def test_stage2_no_longer_accepts_book_pattern_without_evidence(self) -> None:
        record = {
            "status": "review",
            "filename": "10.1007_978-3-030-12345-6.pdf",
            "doi": "10.1007/978-3-030-12345-6",
            "doi_source": "filename_doi",
            "title": "",
            "authors": "",
        }
        decision = stage2_decide(record)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.decision, "stage2_needs_authority")

    def test_stage3_accepts_resolved_book_doi_from_filename(self) -> None:
        record = {
            "status": "review",
            "filename": "10.1007_978-3-030-12345-6.pdf",
            "doi": "10.1007/978-3-030-12345-6",
            "doi_source": "filename_doi",
            "title": "",
            "authors": "",
        }
        authority = {
            "resolved": True,
            "authority_doi": "10.1007/978-3-030-12345-6",
            "authority_title": "Some Battery Handbook",
            "authority_authors": [],
            "error": "",
        }
        decision = stage3_decide(record, authority)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.stage, "stage3")

    def test_ecs_variant_replaces_underscores_with_slashes(self) -> None:
        self.assertEqual(
            ecs_variants("10.1149/ma2013-01_4_233"),
            ["10.1149/MA2013-01/4/233"],
        )

    def test_postprocess_updates_record_to_ok_and_keeps_original_fields(self) -> None:
        record = {
            "status": "review",
            "method": "filename_only_candidate_requires_validation",
            "doi": "10.1021/acsenergylett.4c01234",
            "confidence": "review",
            "title": "A Practical Battery Paper",
            "authors": "Ada Lovelace; Grace Hopper",
            "doi_validation_score": '{"title_score":0.89,"author_score":0.8}',
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            summary = postprocess_review_records([record], Path(tmpdir), use_authority=False)
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["original_status"], "review")
        self.assertEqual(record["postprocess_decision"], "stage2_accept_probable")
        self.assertEqual(summary["accepted_rows"], 1)

    def test_apply_decision_uses_authority_canonical_doi(self) -> None:
        record = {
            "status": "review",
            "method": "filename_only_candidate_requires_validation",
            "doi": "10.1149/ma2013-01_4_233",
            "confidence": "review",
        }
        authority = {
            "resolved": True,
            "authority_doi": "10.1149/MA2013-01/4/233",
            "authority_title": "Electrochemical Society Abstract",
            "authority_authors": [],
            "error": "",
        }
        decision = PostprocessDecision(
            "stage4_accept_probable",
            "stage4",
            True,
            "medium",
            "resolved after correction",
            authority=authority,
            corrected_doi="10.1149/MA2013-01/4/233",
        )
        apply_decision(record, decision)
        self.assertEqual(record["doi"], "10.1149/MA2013-01/4/233")


if __name__ == "__main__":
    unittest.main()
