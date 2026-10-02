"""
repositories — Database repositories package providing concrete SQL implementations of storage interfaces.
"""

from app.storage.repositories.base import (
    StorageException,
    ConcurrencyConflictException,
    EntityNotFoundException,
    DuplicateEntityException,
)
from app.storage.repositories.tenant_repository import (
    TenantRepositoryInterface,
    SQLTenantRepository,
)
from app.storage.repositories.user_repository import (
    SQLUserRepository,
)
from app.storage.repositories.document_repository import (
    DocumentRepositoryInterface,
    SQLDocumentRepository,
)
from app.storage.repositories.permission_repository import (
    PermissionRepositoryInterface,
    SQLPermissionRepository,
)
from app.storage.repositories.chunk_repository import (
    ChunkRepositoryInterface,
    SQLChunkRepository,
)
from app.storage.repositories.audit_repository import (
    AuditRepositoryInterface,
    SQLAuditRepository,
)
from app.storage.repositories.connector_repository import (
    ConnectorRepositoryInterface,
    SQLConnectorRepository,
)
from app.storage.repositories.sync_run_repository import (
    SyncRunRepositoryInterface,
    SQLSyncRunRepository,
)
from app.storage.repositories.task_repository import (
    SQLTaskRepository,
    SQLOutboxRepository,
)
from app.storage.repositories.migration_repository import (
    SQLStorageMigrationRepository,
)

__all__ = [
    "StorageException",
    "ConcurrencyConflictException",
    "EntityNotFoundException",
    "DuplicateEntityException",
    "TenantRepositoryInterface",
    "SQLTenantRepository",
    "SQLUserRepository",
    "DocumentRepositoryInterface",
    "SQLDocumentRepository",
    "PermissionRepositoryInterface",
    "SQLPermissionRepository",
    "ChunkRepositoryInterface",
    "SQLChunkRepository",
    "AuditRepositoryInterface",
    "SQLAuditRepository",
    "ConnectorRepositoryInterface",
    "SQLConnectorRepository",
    "SyncRunRepositoryInterface",
    "SQLSyncRunRepository",
    "SQLTaskRepository",
    "SQLOutboxRepository",
    "SQLStorageMigrationRepository",
]
