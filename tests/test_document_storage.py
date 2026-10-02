"""
test_document_storage.py — Unit tests for physical document blob storage abstractions.
Tests LocalFilesystemStorage save, get, delete, exists, and tenant folder isolation.
"""

import os
import sys
import pytest
import tempfile
import shutil

# Ensure backend directory is in sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
backend_dir = os.path.join(parent_dir, "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.storage.blob.storage import LocalFilesystemStorage


@pytest.fixture
def temp_storage():
    tmp_dir = tempfile.mkdtemp()
    storage = LocalFilesystemStorage(base_dir=tmp_dir)
    yield storage
    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_blob_storage_lifecycle(temp_storage):
    tenant_id = "company_a"
    content = b"This is a confidential enterprise report."
    filename = "report_q3.txt"

    # Save
    path = temp_storage.save(content, filename, tenant_id)
    assert os.path.exists(path)
    assert tenant_id in path

    # Exists
    assert temp_storage.exists(path) is True
    assert temp_storage.exists("/non/existent/path") is False

    # Get
    retrieved = temp_storage.get(path)
    assert retrieved == content

    # Get size
    size = temp_storage.get_size(path)
    assert size == len(content)

    # Delete
    deleted = temp_storage.delete(path)
    assert deleted is True
    assert temp_storage.exists(path) is False

    # Non-existent delete returns False
    assert temp_storage.delete(path) is False


def test_tenant_folder_isolation(temp_storage):
    content1 = b"Tenant A Document"
    content2 = b"Tenant B Document"

    path_a = temp_storage.save(content1, "doc.txt", "tenant_a")
    path_b = temp_storage.save(content2, "doc.txt", "tenant_b")

    assert os.path.dirname(path_a) != os.path.dirname(path_b)
    assert "tenant_a" in path_a
    assert "tenant_b" in path_b
    assert temp_storage.get(path_a) == content1
    assert temp_storage.get(path_b) == content2
