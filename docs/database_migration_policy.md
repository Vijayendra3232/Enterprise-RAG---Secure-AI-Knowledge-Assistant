# Enterprise RAG — Database Migration & Rollback Policy

## 1. Zero-Downtime Migration Principles

In a production environment with Multi-AZ PostgreSQL and ECS Fargate services, schema changes must never cause downtime or race conditions between old and new application instances during rolling deployments.

We enforce the **Expand/Contract (Parallel Run) Pattern**:

```
1. EXPAND   ──> Add new columns / tables with default values or nullable constraints (Backwards-compatible).
2. DEPLOY   ──> Deploy new application code (ECS API & Workers) that can read/write new schema while supporting old data.
3. MIGRATE  ──> Backfill existing records in the background if necessary.
4. VERIFY   ──> Confirm application health and data integrity.
5. CONTRACT ──> Drop obsolete columns/tables in a subsequent independent migration release once old code is retired.
```

---

## 2. Decoupled Rollback Policy (Mandatory Rule)

### The Invariant
**When an application deployment fails health verification, the deployment orchestrator (`scripts/deploy.py` or CI/CD) must revert ECS task definitions and search aliases, but NEVER automatically execute `alembic downgrade`.**

### Why Automatic Database Downgrade is Forbidden
1. **Irreversible Data Loss**: Reverting a migration automatically may execute `DROP COLUMN` or `DROP TABLE`, permanently destroying data written by users or ingestion tasks during the deployment window.
2. **Backward Compatibility Guarantee**: Because all migrations follow the **EXPAND** phase first, existing application code from the prior release can safely run against the newly expanded schema without errors.
3. **Multi-Instance Concurrency**: Running an automated downgrade while active worker nodes or API tasks are still shutting down creates catastrophic schema mismatch exceptions.

---

## 3. Operational Manual Database Rollback Procedure

If a migration introduced a flaw that requires schema remediation:

1. **Step 1: Check Current Database Revision**
   ```bash
   alembic current
   ```

2. **Step 2: Inspect Alembic History**
   ```bash
   alembic history --verbose
   ```

3. **Step 3: Analyze Data Safety of Downgrade**
   Ensure no active queries or newly inserted records depend on the columns or constraints to be modified.

4. **Step 4: Execute Controlled Downgrade or Forward Fix**
   - Preferred: Author a new forward migration (`alembic revision -m "fix_issue"`) to repair the schema forward without dropping data.
   - Fallback (Emergency only with DBA sign-off):
     ```bash
     alembic downgrade <target_revision>
     ```
