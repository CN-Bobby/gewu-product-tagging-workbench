from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


class Storage:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def project_dir(self, project_id):
        return self.root / "projects" / project_id

    def save_raw(self, project_id, filename, data):
        digest = self.digest(data)
        safe = Path(filename).name.replace(" ", "_")
        path = self.project_dir(project_id) / "raw" / (digest[:12] + "_" + safe)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        return path, digest

    def revision_dir(self, project_id, run_id, revision):
        path = self.project_dir(project_id) / "runs" / run_id / ("rev-%04d" % revision)
        (path / "cache").mkdir(parents=True, exist_ok=True)
        (path / "output").mkdir(parents=True, exist_ok=True)
        return path

    def clone_revision(self, project_id, run_id, source_revision, target_revision):
        source = self.revision_dir(project_id, run_id, source_revision)
        target = self.project_dir(project_id) / "runs" / run_id / ("rev-%04d" % target_revision)
        if target.exists():
            raise FileExistsError(str(target))
        shutil.copytree(source, target)
        return target

    def artifact(self, project_id, run_id, revision, kind):
        workdir = self.revision_dir(project_id, run_id, revision)
        candidates = {
            "html": list((workdir / "output").glob("*.html")),
            "excel": list((workdir / "output").glob("*.xlsx")),
            "analysis": [workdir / "cache" / "analysis.json"],
            "quality": [workdir / "cache" / "quality_report.json"],
            "calls": [workdir / "cache" / "model_calls.json"],
        }.get(kind, [])
        return next((path for path in candidates if path.exists()), None)
