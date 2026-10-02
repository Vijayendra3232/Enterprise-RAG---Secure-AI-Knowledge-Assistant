"""
test_connectors_local.py — Tests for local connector adapter, file discovery, SHA-256 hashing,
sidecar ACL parsing, and fail-closed handling of missing/malformed ACLs.
"""

import json
import os
import sys
import tempfile
import pytest

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
backend_dir = os.path.join(parent_dir, "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.connectors.adapters.local import LocalConnector
from app.connectors.registry import connector_registry
from app.connectors.base import PrincipalType, PermissionEffect
from app.connectors.errors import SourceUnavailableError


@pytest.fixture
def local_data_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create subfolders (note: folder names MUST NOT determine permissions)
        eng_dir = os.path.join(tmpdir, "engineering")
        fin_dir = os.path.join(tmpdir, "finance")
        hr_dir = os.path.join(tmpdir, "hr")
        os.makedirs(eng_dir, exist_ok=True)
        os.makedirs(fin_dir, exist_ok=True)
        os.makedirs(hr_dir, exist_ok=True)

        # 1. Document with explicit role-based ACL
        eng_file = os.path.join(eng_dir, "microservices.md")
        with open(eng_file, "w", encoding="utf-8") as f:
            f.write("# Engineering Architecture\nMicroservices must maintain 80% test coverage.")

        eng_acl = os.path.join(eng_dir, "microservices.md.acl.json")
        with open(eng_acl, "w", encoding="utf-8") as f:
            json.dump({
                "permissions": [
                    {"principal_type": "ROLE", "principal_id": "ENGINEERING", "effect": "ALLOW"},
                    {"principal_type": "ROLE", "principal_id": "GUEST", "effect": "DENY"}
                ],
                "permission_status": "KNOWN"
            }, f)

        # 2. Document with user and group ACL
        fin_file = os.path.join(fin_dir, "q3_budget.txt")
        with open(fin_file, "w", encoding="utf-8") as f:
            f.write("Q3 Secret Budget is 50 crore INR.")

        fin_acl = os.path.join(fin_dir, "q3_budget.txt.acl.json")
        with open(fin_acl, "w", encoding="utf-8") as f:
            json.dump({
                "permissions": [
                    {"principal_type": "USER", "principal_id": "cfo_user", "effect": "ALLOW"},
                    {"principal_type": "GROUP", "principal_id": "finance_execs", "effect": "ALLOW"},
                    {"principal_type": "ROLE", "principal_id": "ENGINEERING", "effect": "DENY"}
                ],
                "permission_status": "KNOWN"
            }, f)

        # 3. Document with missing ACL (must fail closed -> UNKNOWN)
        hr_file = os.path.join(hr_dir, "handbook.txt")
        with open(hr_file, "w", encoding="utf-8") as f:
            f.write("Employee handbook and company policies.")

        # 4. Document with malformed ACL (must fail closed -> UNKNOWN)
        bad_file = os.path.join(hr_dir, "corrupted.txt")
        with open(bad_file, "w", encoding="utf-8") as f:
            f.write("Confidential memo with corrupted ACL.")

        bad_acl = os.path.join(hr_dir, "corrupted.txt.acl.json")
        with open(bad_acl, "w", encoding="utf-8") as f:
            f.write("{ invalid json formatting ... ")

        yield tmpdir


def test_connector_registry():
    assert "local" in connector_registry.list_supported_types()
    cls = connector_registry.get("local")
    assert cls is LocalConnector

    conn = connector_registry.create(
        connector_type="local",
        tenant_id="tenant_test",
        connector_id="conn_1",
        config={"directory_path": "some/path"},
    )
    assert isinstance(conn, LocalConnector)
    assert conn.tenant_id == "tenant_test"


def test_local_connector_discovery_and_metadata(local_data_dir):
    conn = LocalConnector(
        tenant_id="tenant_test",
        connector_id="conn_local",
        config={"directory_path": local_data_dir},
    )

    docs = conn.list_documents()
    # 4 valid text documents discovered (sidecar .acl.json files must be ignored as standalone docs)
    assert len(docs) == 4

    doc_names = {d.name for d in docs}
    assert doc_names == {"microservices.md", "q3_budget.txt", "handbook.txt", "corrupted.txt"}

    # Verify document attributes
    eng_doc = next(d for d in docs if d.name == "microservices.md")
    assert eng_doc.source_type == "local"
    assert "engineering/microservices.md" in eng_doc.source_id.replace("\\", "/")
    assert len(eng_doc.content_hash) == 64
    assert eng_doc.size_bytes > 0
    assert eng_doc.modified_at is not None

    # Verify ACL parsing
    assert eng_doc.acl is not None
    assert eng_doc.acl.permission_status == "KNOWN"
    assert len(eng_doc.acl.permissions) == 2

    p0 = eng_doc.acl.permissions[0]
    assert p0.principal.principal_type == PrincipalType.ROLE
    assert p0.principal.principal_id == "ENGINEERING"
    assert p0.effect == PermissionEffect.ALLOW

    p1 = eng_doc.acl.permissions[1]
    assert p1.principal.principal_type == PrincipalType.ROLE
    assert p1.principal.principal_id == "GUEST"
    assert p1.effect == PermissionEffect.DENY


def test_sidecar_acl_group_and_user_parsing(local_data_dir):
    conn = LocalConnector(
        tenant_id="tenant_test",
        connector_id="conn_local",
        config={"directory_path": local_data_dir},
    )
    docs = conn.list_documents()
    fin_doc = next(d for d in docs if d.name == "q3_budget.txt")

    assert fin_doc.acl is not None
    assert fin_doc.acl.permission_status == "KNOWN"
    assert len(fin_doc.acl.permissions) == 3

    p_user = next(p for p in fin_doc.acl.permissions if p.principal.principal_type == PrincipalType.USER)
    assert p_user.principal.principal_id == "cfo_user"
    assert p_user.effect == PermissionEffect.ALLOW

    p_group = next(p for p in fin_doc.acl.permissions if p.principal.principal_type == PrincipalType.GROUP)
    assert p_group.principal.principal_id == "finance_execs"
    assert p_group.effect == PermissionEffect.ALLOW

    p_deny = next(p for p in fin_doc.acl.permissions if p.effect == PermissionEffect.DENY)
    assert p_deny.principal.principal_type == PrincipalType.ROLE
    assert p_deny.principal.principal_id == "ENGINEERING"


def test_missing_and_malformed_acl_fails_closed_to_unknown(local_data_dir):
    conn = LocalConnector(
        tenant_id="tenant_test",
        connector_id="conn_local",
        config={"directory_path": local_data_dir},
    )
    docs = conn.list_documents()

    # Missing ACL -> UNKNOWN
    handbook_doc = next(d for d in docs if d.name == "handbook.txt")
    assert handbook_doc.acl is not None
    assert handbook_doc.acl.permission_status == "UNKNOWN"
    assert len(handbook_doc.acl.permissions) == 0

    # Malformed ACL -> UNKNOWN
    corrupted_doc = next(d for d in docs if d.name == "corrupted.txt")
    assert corrupted_doc.acl is not None
    assert corrupted_doc.acl.permission_status == "UNKNOWN"
    assert len(corrupted_doc.acl.permissions) == 0


def test_invalid_directory_path_raises_source_unavailable():
    conn = LocalConnector(
        tenant_id="tenant_test",
        connector_id="conn_invalid",
        config={"directory_path": "non_existent_folder_xyz_123"},
    )
    with pytest.raises(SourceUnavailableError):
        conn.list_documents()
