from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from bosgenesis_mop_execution_agent.api.service import (
    _materialize_uploaded_bundle_source,
)


def test_uploaded_bundle_is_materialized_under_persistent_artifact_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    artifact_root = tmp_path / "persistent-artifacts"
    monkeypatch.setenv("ARTIFACT_ROOT_PATH", str(artifact_root))
    content = b"PK\x03\x04persistent-bundle-fixture"
    digest = hashlib.sha256(content).hexdigest()
    source = {
        "type": "uploaded_archive",
        "filename": "mop-bundle.zip",
        "archive_base64": base64.b64encode(content).decode("ascii"),
        "size_bytes": len(content),
        "sha256": digest,
    }

    materialized = _materialize_uploaded_bundle_source(source)
    archive = Path(materialized["value"])

    assert materialized["type"] == "uploaded_zip"
    assert archive == artifact_root / "uploads" / digest / "mop-bundle.zip"
    assert archive.read_bytes() == content
    assert _materialize_uploaded_bundle_source(source) == materialized
