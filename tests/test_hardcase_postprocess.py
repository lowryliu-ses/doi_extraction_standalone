import unittest

from doi_pipeline.hardcase_postprocess import (
    Authority,
    Candidate,
    apply_hardcase_result,
    authority_title_coverage,
    decision_for,
    doi_variants_from_token,
)


class HardcasePostprocessTests(unittest.TestCase):
    def test_ecs_issn_variant_replaces_second_separator(self) -> None:
        self.assertIn(
            "10.1149/1945-7111/ac6aea",
            [candidate.doi for candidate in doi_variants_from_token("10.1149_1945-7111_ac6aea")],
        )

    def test_repository_variant_uses_path_slashes_and_uppercase(self) -> None:
        self.assertIn(
            "10.5445/IR/1000091096",
            [candidate.doi for candidate in doi_variants_from_token("10.5445_ir_1000091096")],
        )

    def test_authority_title_coverage_accepts_pdf_text_match(self) -> None:
        self.assertEqual(
            authority_title_coverage(
                "Bipolar Membranes with an Electrospun 3D Junction",
                "This report describes Bipolar Membranes with an Electrospun 3D Junction in detail.",
            ),
            1.0,
        )

    def test_decision_accepts_authority_title_in_pdf_text(self) -> None:
        record = {"title": "This report was prepared as an account of work sponsored by an agency", "authors": ""}
        candidate = Candidate("10.2172/2516710", "filename_variant", "OSTI DOI")
        authority = Authority(
            True,
            "10.2172/2516710",
            source="osti",
            authority_doi="10.2172/2516710",
            title="Bipolar Membranes with an Electrospun 3D Junction",
            authors=["Peter Pintauro"],
        )
        result = decision_for(record, candidate, authority, "Bipolar Membranes with an Electrospun 3D Junction")
        self.assertEqual(result["hardcase_accept"], "1")
        self.assertEqual(result["hardcase_confidence"], "high")

    def test_apply_hardcase_result_updates_record_and_bad_title(self) -> None:
        record = {
            "status": "review",
            "method": "filename_only_candidate_requires_validation",
            "doi": "10.1149/1945-7111_ac6aea",
            "confidence": "review",
            "title": "Accepted Manuscript",
            "authors": "",
            "metadata_source": "pdf",
        }
        result = {
            "hardcase_accept": "1",
            "hardcase_confidence": "medium",
            "hardcase_reason": "filename DOI resolves and local title is missing/header noise",
            "hardcase_final_doi": "10.1149/1945-7111/ac6aea",
            "hardcase_candidate_doi": "10.1149/1945-7111/ac6aea",
            "hardcase_candidate_source": "filename_variant",
            "hardcase_candidate_reason": "ECS ISSN/article DOI",
            "hardcase_title_score": "0.0000",
            "hardcase_author_score": "0.0000",
            "hardcase_pdf_title_coverage": "0.0000",
            "hardcase_local_title_bad": "1",
            "authority_source": "doi.org",
            "authority_title_new": "Review - Carbon Cloth as a Versatile Electrode",
            "authority_authors_new": "Maria Leon; Jose Nava",
            "authority_year_new": "2022",
            "authority_container_new": "Journal of The Electrochemical Society",
            "authority_publisher_new": "The Electrochemical Society",
        }
        apply_hardcase_result(record, result)
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["doi"], "10.1149/1945-7111/ac6aea")
        self.assertEqual(record["title"], "Review - Carbon Cloth as a Versatile Electrode")
        self.assertEqual(record["authors"], ["Maria Leon", "Jose Nava"])


if __name__ == "__main__":
    unittest.main()
