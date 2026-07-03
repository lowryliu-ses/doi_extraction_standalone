import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from doi_pipeline.validators import MetadataValidator, load_cache


class ValidatorCacheTests(unittest.TestCase):
    def test_load_cache_skips_persisted_error_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.jsonl"
            path.write_text(
                json.dumps({"key": "crossref:doi:10.1/good", "value": {"status": "ok"}})
                + "\n"
                + json.dumps({"key": "crossref:doi:10.1/bad", "value": {"error": "HTTPError: 429"}})
                + "\n",
                encoding="utf-8",
            )
            cache = load_cache(path)
        self.assertIn("crossref:doi:10.1/good", cache)
        self.assertNotIn("crossref:doi:10.1/bad", cache)

    def test_transient_error_not_persisted_but_kept_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.jsonl"
            validator = MetadataValidator(path, min_interval=0.0)
            with mock.patch(
                "doi_pipeline.validators.request_json",
                side_effect=OSError("timed out"),
            ) as request:
                value = validator.cached_json("crossref:doi:10.1/x", "https://example.invalid")
                again = validator.cached_json("crossref:doi:10.1/x", "https://example.invalid")
            self.assertIn("error", value)
            self.assertEqual(value, again)
            self.assertEqual(request.call_count, 1)
            self.assertFalse(path.exists())

    def test_success_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.jsonl"
            validator = MetadataValidator(path, min_interval=0.0)
            with mock.patch(
                "doi_pipeline.validators.request_json",
                return_value={"status": "ok", "message": {}},
            ):
                validator.cached_json("crossref:doi:10.1/y", "https://example.invalid")
            self.assertIn("crossref:doi:10.1/y", load_cache(path))

    def test_mailto_added_to_user_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            validator = MetadataValidator(Path(tmpdir) / "cache.jsonl", mailto="me@example.org")
        self.assertIn("mailto:me@example.org", validator.user_agent)


if __name__ == "__main__":
    unittest.main()
