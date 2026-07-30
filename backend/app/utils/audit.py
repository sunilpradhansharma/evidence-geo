"""Append-only audit log writer (SE-003)."""
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.utils.logging import redact_phi


async def write_audit(
    db: AsyncSession,
    *,
    role: str,
    event: str,
    run_id: str | None = None,
    question_id: str | None = None,
    llm_target: str | None = None,
    http_status: int | None = None,
    tokens: int | None = None,
    context: dict | None = None,
    commit: bool = True,
) -> None:
    """Write a credential-redacted audit entry. Never updates existing rows."""
    ctx_json = None
    if context is not None:
        ctx_json = redact_phi(json.dumps(context, default=str))

    entry = AuditLog(
        role=role,
        event=event,
        run_id=run_id,
        question_id=question_id,
        llm_target=llm_target,
        http_status=http_status,
        tokens=tokens,
        context=ctx_json,
    )
    db.add(entry)
    if commit:
        await db.commit()
