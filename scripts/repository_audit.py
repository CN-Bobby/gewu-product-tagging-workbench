"""Audit tracked repository content before a GitHub push.

The audit intentionally checks names and high-confidence credential patterns only;
test fixtures may contain words such as ``secret`` without being credentials.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATH = re.compile(
    r"(?i)(^|/)(storage|releases)(/|$)|(?:\.xlsx|\.xlsm|\.db|\.sqlite(?:3)?|\.png|\.jpe?g|\.webp|\.gif)$|(?:^|/)\.env(?:\.(?!example(?:$|/))|$)"
)
SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)authorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~-]{20,}"),
]
SENSITIVE_SAMPLE = re.compile(r"(?:\bB[0-9][A-Z0-9]{8}\b|\bROW-\d+\b|(?:D|C):\\\\)")
SYNTHETIC_ASINS = {
    'B0XXXXXXXX', 'B000000000', 'B000000001', 'B000000002', 'B000000003',
    'B000000004', 'B000000005', 'B000000006', 'B012345678', 'B087654321', 'B0CHILD001',
    'B0CHILD002', 'B0PARENT01', 'B0PARENT02',
}


def tracked() -> list[str]:
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [x for x in raw.decode("utf-8").split("\0") if x]


def main() -> int:
    paths = tracked()
    forbidden = [p for p in paths if FORBIDDEN_PATH.search(p) and not p.startswith('tests/fixtures/')]
    secret_hits: list[str] = []
    sample_hits: list[str] = []
    for name in paths:
        path = ROOT / name
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024 or path.suffix.lower() in {'.xlsx', '.xlsm', '.db', '.sqlite', '.sqlite3', '.png', '.jpg', '.jpeg', '.webp', '.gif'}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            secret_hits.append(name)
        unsafe_samples = [match.group(0) for match in SENSITIVE_SAMPLE.finditer(text)
                          if not match.group(0).startswith('B') or match.group(0) not in SYNTHETIC_ASINS]
        if name != 'scripts/repository_audit.py' and not name.startswith('docs/vendor/') and unsafe_samples:
            sample_hits.append(name)
    errors: list[str] = []
    if forbidden:
        errors.append("forbidden tracked paths: " + ", ".join(sorted(forbidden)))
    if secret_hits:
        errors.append("credential-like content: " + ", ".join(sorted(set(secret_hits))))
    if sample_hits:
        errors.append("unsanitized customer sample references: " + ", ".join(sorted(set(sample_hits))))
    report = {"tracked_files": len(paths), "forbidden_paths": forbidden, "secret_hits": secret_hits, "sample_hits": sample_hits}
    print(report)
    if errors:
        for error in errors:
            print("ERROR: " + error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
