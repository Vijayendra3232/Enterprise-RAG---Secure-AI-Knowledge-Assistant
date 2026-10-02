import os
import sys
import pytest

# Ensure backend directory is in sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
backend_dir = os.path.join(parent_dir, "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.auth.models import User
from app.authorization.roles import Role
from app.authorization.permissions import Permission
from app.authorization.context import AuthorizationContext
from app.authorization.policy import AuthorizationPolicy
from app.authorization.filters import is_document_accessible


def test_role_permissions_mapping():
    # Admin has all permissions
    admin_perms = AuthorizationPolicy.get_permissions(Role.ADMIN.value)
    assert Permission.DOCUMENT_READ in admin_perms
    assert Permission.DOCUMENT_UPLOAD in admin_perms
    assert Permission.DOCUMENT_DELETE in admin_perms
    assert Permission.USER_MANAGE in admin_perms
    assert Permission.SETTINGS_MANAGE in admin_perms

    # Viewer only has read
    viewer_perms = AuthorizationPolicy.get_permissions(Role.VIEWER.value)
    assert Permission.DOCUMENT_READ in viewer_perms
    assert Permission.DOCUMENT_UPLOAD not in viewer_perms
    assert Permission.DOCUMENT_DELETE not in viewer_perms

    # User / Engineering has read, upload, delete
    user_perms = AuthorizationPolicy.get_permissions(Role.ENGINEERING.value)
    assert Permission.DOCUMENT_READ in user_perms
    assert Permission.DOCUMENT_UPLOAD in user_perms
    assert Permission.USER_MANAGE not in user_perms


def test_authorization_policy_can():
    admin = User(user_id="a1", tenant_id="t1", email="a@t.com", name="Admin", role="ADMIN")
    user = User(user_id="u1", tenant_id="t1", email="u@t.com", name="User", role="USER")
    viewer = User(user_id="v1", tenant_id="t1", email="v@t.com", name="Viewer", role="VIEWER")
    inactive = User(user_id="i1", tenant_id="t1", email="i@t.com", name="Inactive", role="ADMIN", is_active=False)

    assert AuthorizationPolicy.can(admin, Permission.USER_MANAGE) is True
    assert AuthorizationPolicy.can(user, Permission.USER_MANAGE) is False
    assert AuthorizationPolicy.can(user, Permission.DOCUMENT_READ) is True
    assert AuthorizationPolicy.can(viewer, Permission.DOCUMENT_READ) is True
    assert AuthorizationPolicy.can(viewer, Permission.DOCUMENT_UPLOAD) is False
    assert AuthorizationPolicy.can(inactive, Permission.DOCUMENT_READ) is False


def test_document_deletion_policy():
    admin = User(user_id="admin_a", tenant_id="company_a", email="adm@a.com", name="Admin", role="ADMIN")
    user_a = User(user_id="user_a", tenant_id="company_a", email="ua@a.com", name="User A", role="ENGINEERING")
    user_b = User(user_id="user_b", tenant_id="company_a", email="ub@a.com", name="User B", role="FINANCE")
    viewer = User(user_id="viewer_a", tenant_id="company_a", email="v@a.com", name="Viewer", role="VIEWER")

    doc_a = {"document_id": "d1", "tenant_id": "company_a", "owner_id": "user_a"}
    doc_b = {"document_id": "d2", "tenant_id": "company_a", "owner_id": "user_b"}
    doc_other_tenant = {"document_id": "d3", "tenant_id": "company_b", "owner_id": "user_c"}

    # User A can delete own document
    assert AuthorizationPolicy.can_delete_document(user_a, doc_a) is True
    # User A CANNOT delete User B's document
    assert AuthorizationPolicy.can_delete_document(user_a, doc_b) is False
    # User A CANNOT delete cross-tenant document
    assert AuthorizationPolicy.can_delete_document(user_a, doc_other_tenant) is False

    # Admin in Company A can delete any document in Company A
    assert AuthorizationPolicy.can_delete_document(admin, doc_a) is True
    assert AuthorizationPolicy.can_delete_document(admin, doc_b) is True
    # Admin in Company A CANNOT delete Company B document (Strict Tenant Boundary)
    assert AuthorizationPolicy.can_delete_document(admin, doc_other_tenant) is False

    # Viewer cannot delete anything
    assert AuthorizationPolicy.can_delete_document(viewer, doc_a) is False


def test_is_document_accessible_multi_tenant_isolation():
    ctx_comp_a = AuthorizationContext(user_id="user_a", tenant_id="company_a", role="ENGINEERING")
    ctx_comp_b = AuthorizationContext(user_id="user_c", tenant_id="company_b", role="ENGINEERING")

    doc_comp_a = {"document_id": "doc_1", "tenant_id": "company_a", "access_level": "PUBLIC"}
    doc_comp_b = {"document_id": "doc_2", "tenant_id": "company_b", "access_level": "PUBLIC"}

    # Company A user accessing Company A public document -> Allowed
    assert is_document_accessible(doc_comp_a, ctx_comp_a) is True
    # Company A user accessing Company B public document -> Strictly DENIED
    assert is_document_accessible(doc_comp_b, ctx_comp_a) is False
    # Company B user accessing Company B public document -> Allowed
    assert is_document_accessible(doc_comp_b, ctx_comp_b) is True
    # Company B user accessing Company A public document -> Strictly DENIED
    assert is_document_accessible(doc_comp_a, ctx_comp_b) is False


def test_is_document_accessible_role_based_access():
    ctx_eng = AuthorizationContext(user_id="user_a", tenant_id="company_a", role="ENGINEERING")
    ctx_fin = AuthorizationContext(user_id="user_b", tenant_id="company_a", role="FINANCE")
    ctx_admin = AuthorizationContext(user_id="admin_a", tenant_id="company_a", role="ADMIN")

    eng_doc = {
        "document_id": "eng_doc_1",
        "tenant_id": "company_a",
        "access_level": "ROLE",
        "allowed_roles": ["ENGINEERING"]
    }
    fin_doc = {
        "document_id": "fin_doc_1",
        "tenant_id": "company_a",
        "access_level": "ROLE",
        "allowed_roles": ["FINANCE"]
    }

    # Engineering doc access
    assert is_document_accessible(eng_doc, ctx_eng) is True
    assert is_document_accessible(eng_doc, ctx_fin) is False
    assert is_document_accessible(eng_doc, ctx_admin) is True

    # Finance doc access
    assert is_document_accessible(fin_doc, ctx_fin) is True
    assert is_document_accessible(fin_doc, ctx_eng) is False
    assert is_document_accessible(fin_doc, ctx_admin) is True


def test_is_document_accessible_user_specific_and_private():
    ctx_user_a = AuthorizationContext(user_id="user_a", tenant_id="company_a", role="USER")
    ctx_user_b = AuthorizationContext(user_id="user_b", tenant_id="company_a", role="USER")

    user_specific_doc = {
        "document_id": "doc_user_a",
        "tenant_id": "company_a",
        "access_level": "USER",
        "allowed_user_ids": ["user_a"]
    }
    private_doc = {
        "document_id": "doc_priv_a",
        "tenant_id": "company_a",
        "access_level": "PRIVATE",
        "owner_id": "user_a"
    }

    # User-specific
    assert is_document_accessible(user_specific_doc, ctx_user_a) is True
    assert is_document_accessible(user_specific_doc, ctx_user_b) is False

    # Private
    assert is_document_accessible(private_doc, ctx_user_a) is True
    assert is_document_accessible(private_doc, ctx_user_b) is False


def test_is_document_accessible_unknown_permission_fails_safe():
    ctx_user_a = AuthorizationContext(user_id="user_a", tenant_id="company_a", role="USER")
    ctx_user_b = AuthorizationContext(user_id="user_b", tenant_id="company_a", role="USER")

    unknown_doc = {
        "document_id": "doc_unknown",
        "tenant_id": "company_a",
        "permission_status": "UNKNOWN",
        "owner_id": "user_a"
    }

    # Owner can access even if status is unknown
    assert is_document_accessible(unknown_doc, ctx_user_a) is True
    # Non-owner is denied by fail-safe restrictive policy
    assert is_document_accessible(unknown_doc, ctx_user_b) is False
