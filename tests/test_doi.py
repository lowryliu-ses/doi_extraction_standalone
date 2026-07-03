import unittest

from doi_pipeline.doi import (
    DoiCandidate,
    choose_candidate,
    decode_bytes,
    find_dois,
    is_plausible_doi,
    normalize_doi,
    raw_confidence_for_source,
)


class NormalizeDoiTests(unittest.TestCase):
    def test_strip_trailing_bracket(self) -> None:
        self.assertEqual(
            normalize_doi("https://doi.org/10.1021/acsnano.6b04019]"),
            "10.1021/acsnano.6b04019",
        )

    def test_wiley_language_pair_prefers_english(self) -> None:
        self.assertEqual(
            normalize_doi("10.1002/anie.201903709; 10.1002/ange.201903709"),
            "10.1002/anie.201903709",
        )
        self.assertEqual(
            [hit.doi for hit in find_dois("10.1002/anie.201903709; 10.1002/ange.201903709", "text")],
            ["10.1002/anie.201903709", "10.1002/anie.201903709"],
        )

    def test_research_square_version(self) -> None:
        self.assertEqual(
            normalize_doi("10.21203/rs.3.rs-123456_v1"),
            "10.21203/rs.3.rs-123456/v1",
        )
        self.assertEqual(
            [hit.doi for hit in find_dois("doi: 10.21203/rs.3.rs-123456_v1", "text")],
            ["10.21203/rs.3.rs-123456/v1"],
        )

    def test_rejects_placeholder_wiley_prompt(self) -> None:
        self.assertFalse(is_plausible_doi("10.1002/((please"))
        self.assertEqual(find_dois("doi: 10.1002/((please add manuscript number))", "text"), [])

    def test_keeps_balanced_elsevier_parentheses(self) -> None:
        self.assertTrue(is_plausible_doi("10.1016/S0378-7753(98)00241-9"))

    def test_reference_section_sources_stay_review(self) -> None:
        self.assertEqual(raw_confidence_for_source("markdown_references"), "review")
        self.assertEqual(raw_confidence_for_source("ocr_references"), "review")

    def test_decode_bytes_keeps_non_utf8_doi_bytes(self) -> None:
        data = b"\xa9 2020 doi: 10.1016/j.jpowsour.2020.228999"
        self.assertIn("10.1016/j.jpowsour.2020.228999", decode_bytes(data))

    def test_candidate_selection_filters_invalid_external_candidate(self) -> None:
        doi, confidence, decision = choose_candidate(
            [
                DoiCandidate("10.1002/((please", "grobid_doi", "grobid", "medium", ""),
                DoiCandidate("10.1002/adma.202403521", "filename_doi", "filename", "review", ""),
            ],
            filename_policy="candidate",
        )
        self.assertEqual(doi, "10.1002/adma.202403521")
        self.assertEqual(confidence, "review")
        self.assertEqual(decision, "filename_only_candidate_requires_validation")


if __name__ == "__main__":
    unittest.main()
