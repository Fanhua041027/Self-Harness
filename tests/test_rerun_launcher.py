from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "workflow" / "scripts" / "rerun_clean64_invalid.ps1"


def test_clean64_rerun_launcher_rejects_blank_user_api_key_in_both_modes() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")

    assert source.count("[string]::IsNullOrWhiteSpace(") >= 2
    assert source.count("missing or blank in the Windows user environment") >= 2
    assert 'GetEnvironmentVariable($apiKeyName, "User")' in source
    assert 'GetEnvironmentVariable("$apiKeyName", "User")' in source
    assert "$env:OPENAI_API_KEY = $apiKey" in source
    assert "-EncodedCommand" in source
