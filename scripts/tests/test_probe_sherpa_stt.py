import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "probe-sherpa-stt.py"
SPEC = importlib.util.spec_from_file_location("probe_sherpa_stt", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_word_error_rate_exact_match_ignores_case_and_punctuation():
    assert MODULE._word_error_rate("Hello, Sumi!", "hello sumi") == 0.0


def test_word_error_rate_counts_deletion():
    assert (
        MODULE._word_error_rate(
            "what is natural language processing", "natural language processing"
        )
        == 0.4
    )


def test_word_error_rate_counts_numeric_phrase_loss():
    reference = "35 percent of 40 equals 14"
    hypothesis = "40 equals 14"
    assert MODULE._word_error_rate(reference, hypothesis) == 0.5
