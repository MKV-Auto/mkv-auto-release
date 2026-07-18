import fnmatch
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import hashlib

from core.utils import (
    get_discdb_repo_url,
    get_discdb_repo_branch,
    get_discdb_repo_path,
    get_export_root,
)

REPORT_NAME_TEMPLATE = "devmode-report-{disc_hash}.html"


def _run_git(args: List[str], cwd: Path) -> None:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")


def ensure_repo_checkout() -> Path:
    """
    Ensure the DiscDB data repo is cloned and up to date.
    Returns the path to the repo root.
    """
    repo_path = get_discdb_repo_path()
    url = get_discdb_repo_url()
    branch = get_discdb_repo_branch()

    git_dir = repo_path / ".git"
    if not git_dir.exists():
        repo_path.mkdir(parents=True, exist_ok=True)
        _run_git(["clone", "--branch", branch, url, str(repo_path)], cwd=repo_path.parent)
    else:
        _run_git(["fetch", "origin", branch], cwd=repo_path)
        _run_git(["checkout", branch], cwd=repo_path)
        _run_git(["reset", "--hard", f"origin/{branch}"], cwd=repo_path)
    return repo_path


def _iter_json_files(root: Path):
    for path in root.rglob("*.json"):
        # skip .git internals
        if "/.git/" in str(path):
            continue
        yield path


def locate_disc_dir(repo_root: Path, content_hash: str) -> Optional[Path]:
    """
    Scan the repo for a JSON file containing the disc hash.
    Returns the directory containing that file (typically the release folder).
    """
    data_root = repo_root / "data"
    if not data_root.exists():
        return None
    target = content_hash.upper()
    for path in _iter_json_files(data_root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if target in text or content_hash in text:
            return path.parent
    return None


def _canonical_json_bytes(path: Path) -> bytes:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except Exception:
        return path.read_bytes()


def _file_hash(path: Path) -> str:
    if path.suffix.lower() == ".json":
        payload = _canonical_json_bytes(path)
        return hashlib.sha256(payload).hexdigest()
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gather_files(root: Path, exclude: List[str]) -> Dict[str, Path]:
    files: Dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        rel = str(path.relative_to(root))
        if any(fnmatch.fnmatch(rel, pat) for pat in exclude):
            continue
        files[rel] = path
    return files


def compare_directories(expected_dir: Path, actual_dir: Path, exclude: Optional[List[str]] = None) -> Dict[str, Any]:
    exclude = exclude or []
    expected_files = _gather_files(expected_dir, exclude)
    actual_files = _gather_files(actual_dir, exclude)

    missing = sorted(set(expected_files.keys()) - set(actual_files.keys()))
    extra = sorted(set(actual_files.keys()) - set(expected_files.keys()))
    mismatched: List[Dict[str, str]] = []

    for rel in sorted(set(expected_files.keys()) & set(actual_files.keys())):
        try:
            if _file_hash(expected_files[rel]) != _file_hash(actual_files[rel]):
                mismatched.append({"path": rel, "reason": "content differs"})
        except Exception as exc:
            mismatched.append({"path": rel, "reason": f"compare error: {exc}"})

    status = "matched"
    if missing or extra or mismatched:
        status = "mismatch"

    return {
        "status": status,
        "missing_files": missing,
        "extra_files": extra,
        "mismatched_files": mismatched,
    }


def _format_list(items: List[str]) -> str:
    if not items:
        return "<p>None</p>"
    return "<ul>" + "".join(f"<li><code>{i}</code></li>" for i in items) + "</ul>"


def _format_mismatches(items: List[Dict[str, str]]) -> str:
    if not items:
        return "<p>None</p>"
    return "<ul>" + "".join(f"<li><code>{m.get('path')}</code> — {m.get('reason')}</li>" for m in items) + "</ul>"


def build_validation_report(
    disc_hash: str,
    expected_dir: Path,
    actual_dir: Path,
    diff: Dict[str, Any],
) -> str:
    """Render an HTML report summarizing comparison results."""
    return f"""
<html>
  <head>
    <meta charset="utf-8" />
    <title>Dev Mode Validation Report for {disc_hash}</title>
    <style>
      body {{ font-family: Arial, sans-serif; line-height: 1.4; }}
      code {{ background: #f5f5f5; padding: 2px 4px; border-radius: 3px; }}
    </style>
  </head>
  <body>
    <h1>Dev Mode Validation Report</h1>
    <p><strong>Disc Hash:</strong> {disc_hash}</p>
    <p><strong>Expected:</strong> {expected_dir}</p>
    <p><strong>Actual:</strong> {actual_dir}</p>
    <p><strong>Status:</strong> {diff.get("status")}</p>
    <h2>Missing Files</h2>
    {_format_list(diff.get("missing_files", []))}
    <h2>Extra Files</h2>
    {_format_list(diff.get("extra_files", []))}
    <h2>Mismatched Files</h2>
    {_format_mismatches(diff.get("mismatched_files", []))}
  </body>
</html>
"""


def write_report(actual_dir: Path, disc_hash: str, report_html: str) -> Path:
    """
    Save the HTML report next to the finalized export (excluded from comparisons).
    """
    report_path = actual_dir / REPORT_NAME_TEMPLATE.format(disc_hash=disc_hash)
    report_path.write_text(report_html, encoding="utf-8")
    return report_path


def validate_against_repo(disc_hash: str, actual_dir: Path, exclude: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Ensure repo is available, locate expected dir, compare, and emit report.
    Returns a summary dict including paths and status.
    """
    repo_root = ensure_repo_checkout()
    expected_dir = locate_disc_dir(repo_root, disc_hash)
    if not expected_dir:
        return {
            "status": "error",
            "error": f"Disc hash {disc_hash} not found in repo",
        }

    exclude = exclude or []
    report_name = REPORT_NAME_TEMPLATE.format(disc_hash=disc_hash)
    if report_name not in exclude:
        exclude = exclude + [report_name]

    diff = compare_directories(expected_dir, actual_dir, exclude=exclude)
    report_html = build_validation_report(disc_hash, expected_dir, actual_dir, diff)
    report_path = write_report(actual_dir, disc_hash, report_html)

    summary = {
        "status": diff.get("status"),
        "expected_dir": str(expected_dir),
        "actual_dir": str(actual_dir),
        "report_path": str(report_path),
        "diff": diff,
    }
    return summary
