"""The scanner-safe synthetic fixture must still exercise real redaction."""
import base64
import os
from hermes_lcm.benchmarking.stress import _synthetic_private_key_fixture, run_stress_check
from hermes_lcm.config import LCMConfig
from hermes_lcm.ingest_protection import redact_sensitive_text


def test_synthetic_pem_fixture_still_redacts():
    fixture = _synthetic_private_key_fixture()
    encoded = fixture.splitlines()[1]
    assert base64.b64decode(encoded) == b"hermes-lcm synthetic redaction fixture"
    config = LCMConfig(sensitive_patterns_enabled=True)
    result = redact_sensitive_text(fixture, config)
    assert encoded not in result
    assert "[LCM sensitive redaction:" in result


def test_actual_redaction_stress_case_uses_synthetic_fixture(tmp_path):
    # The legacy stress runner creates directories with the ambient umask;
    # retain private SQLite parents without changing product permissions.
    previous = os.umask(0o077)
    try:
        results = run_stress_check(output_dir=tmp_path / "stress", tier="smoke",
                                   scenarios=["redaction_and_externalization_boundaries"])
    finally:
        os.umask(previous)
    assert results["failure_count"] == 0
    assert results["cases"]["redaction_and_externalization_boundaries"]["ok"] is True
