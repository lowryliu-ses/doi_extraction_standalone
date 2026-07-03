import unittest

from doi_pipeline.ocr import extract_title_authors_from_text


class OcrMetadataTests(unittest.TestCase):
    def test_extracts_title_and_author_line(self) -> None:
        text = """
        Nature Energy
        Article
        A robust interphase enables fast charging lithium metal batteries
        Jian Li1, Maria R. Gomez2 and Q. Wang3,*
        Department of Materials Science, Example University
        Abstract
        The abstract starts here.
        """
        title, authors = extract_title_authors_from_text(text)
        self.assertEqual(title, "A robust interphase enables fast charging lithium metal batteries")
        self.assertEqual(authors, ["Jian Li", "Maria R. Gomez", "Q. Wang"])

    def test_skips_doi_and_front_matter_noise(self) -> None:
        text = """
        doi: 10.1021/example.12345
        www.example-journal.org
        Microsoft Word - manuscript.doc
        Operando imaging of sodium plating in solid electrolytes
        Alice Smith, Bob Chen
        Abstract
        Details follow.
        """
        title, authors = extract_title_authors_from_text(text)
        self.assertEqual(title, "Operando imaging of sodium plating in solid electrolytes")
        self.assertEqual(authors, ["Alice Smith", "Bob Chen"])

    def test_handles_journal_sidebar_inside_title_block(self) -> None:
        text = """
        View Article Online
        D Check for updates. Realizing ultrahigh energy storage performance in
        加 sodium bismuth titanate-based ceramics via local
        Cite this: J. Mater. Chem. A, 2025, 13,
        32857 polarization engineering
        Yiyan Zhou, Shiyu Yang, Ziyang Zhou, Meilin Cao
        Abstract
        Details follow.
        """
        title, authors = extract_title_authors_from_text(text)
        self.assertEqual(
            title,
            "Realizing ultrahigh energy storage performance in sodium bismuth titanate-based ceramics via local polarization engineering",
        )
        self.assertEqual(authors, ["Yiyan Zhou", "Shiyu Yang", "Ziyang Zhou", "Meilin Cao"])


if __name__ == "__main__":
    unittest.main()
