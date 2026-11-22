"""Guards for a public repository: no secrets, local data or personal config in tracked files."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def tracked() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / p for p in out.splitlines()]


def test_no_secret_or_data_files_are_tracked():
    bad = [p.name for p in tracked() if re.search(r"(^|\.)env$|\.(pem|key|p12|har|db|sqlite)$|^user\.txt$", p.name)
           and not p.name.endswith(".example")]
    assert bad == []


SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "real-looking Google API key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "OpenAI/Anthropic key": re.compile(r"\bsk-(ant-)?[A-Za-z0-9_-]{30,}"),
    "Notion token": re.compile(r"\b(secret|ntn)_[A-Za-z0-9]{40,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "filled-in env value": re.compile(r"^(MASTER_KEY|TUNNEL_TOKEN|PCID_CLIENT_SECRET|PCX_API_KEY|ACCESS_AUD)=\S{8,}", re.M),
}


def test_no_secrets_in_tracked_text():
    hits = []
    for p in tracked():
        if p.suffix in (".png", ".jpg", ".ico") or not p.is_file():
            continue
        text = p.read_text(errors="ignore")
        hits += [f"{p.relative_to(ROOT)}: {name}" for name, rx in SECRET_PATTERNS.items() if rx.search(text)]
    assert hits == []
