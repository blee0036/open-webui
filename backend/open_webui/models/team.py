"""Team identity, private integrations and the independent quota ledger."""

from open_webui.internal.db import Base
from sqlalchemy import JSON, BigInteger, Boolean, Column, Index, Integer, Text, UniqueConstraint


class EnterpriseConnection(Base):
    __tablename__ = 'enterprise_connection'
    id = Column(Text, primary_key=True)
    name = Column(Text, nullable=False)
    platform = Column(Text, nullable=False)
    client_id = Column(Text, nullable=False)
    secret = Column(Text, nullable=False)
    redirect_uri = Column(Text, nullable=False)
    tenant_ids = Column(JSON, nullable=False)
    scope = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    revision = Column(Integer, nullable=False, default=1)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)


class EnterpriseIdentity(Base):
    __tablename__ = 'enterprise_identity'
    id = Column(Text, primary_key=True)
    connection_id = Column(Text, nullable=False, index=True)
    tenant_id = Column(Text, nullable=False)
    subject = Column(Text, nullable=False)
    user_id = Column(Text, nullable=False, index=True)
    profile = Column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint('connection_id', 'tenant_id', 'subject', name='uq_enterprise_identity'),)


class EnterpriseLoginState(Base):
    __tablename__ = 'enterprise_login_state'
    id = Column(Text, primary_key=True)
    connection_id = Column(Text, nullable=False)
    revision = Column(Integer, nullable=False)
    browser_nonce = Column(Text, nullable=False)
    expires_at = Column(BigInteger, nullable=False, index=True)


class QuotaPlan(Base):
    __tablename__ = 'quota_plan'
    id = Column(Text, primary_key=True)
    name = Column(Text, nullable=False)
    unit = Column(Text, nullable=False)
    allowance = Column(BigInteger, nullable=False)
    reset = Column(JSON, nullable=False)
    archived = Column(Boolean, nullable=False, default=False)
    # Storage caps in bytes; NULL is unlimited. Uploaded files are a hard limit, sandbox homes a soft one.
    storage_bytes = Column(BigInteger, nullable=True)
    sandbox_bytes = Column(BigInteger, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)


class QuotaAccount(Base):
    __tablename__ = 'quota_account'
    user_id = Column(Text, primary_key=True)
    override_plan_id = Column(Text, nullable=True)
    active_plan_id = Column(Text, nullable=True)
    period_id = Column(Text, nullable=True)
    pending_plan_id = Column(Text, nullable=True)
    pending_inherit = Column(Boolean, nullable=False, default=False)
    pending_at = Column(BigInteger, nullable=True)


class QuotaPeriod(Base):
    __tablename__ = 'quota_period'
    id = Column(Text, primary_key=True)
    user_id = Column(Text, nullable=False, index=True)
    unit = Column(Text, nullable=False)
    reset = Column(JSON, nullable=False)
    starts_at = Column(BigInteger, nullable=False)
    ends_at = Column(BigInteger, nullable=True)
    base = Column(BigInteger, nullable=False)
    bonus = Column(BigInteger, nullable=False, default=0)
    used = Column(BigInteger, nullable=False, default=0)
    reserved = Column(BigInteger, nullable=False, default=0)


class ModelPrice(Base):
    __tablename__ = 'model_price'
    model_id = Column(Text, primary_key=True)
    expression = Column(Text, nullable=False)
    multiplier = Column(Text, nullable=False, default='1')
    revision = Column(Integer, nullable=False, default=1)
    updated_at = Column(BigInteger, nullable=False)


class QuotaEntry(Base):
    __tablename__ = 'quota_entry'
    id = Column(Text, primary_key=True)
    user_id = Column(Text, nullable=False)
    period_id = Column(Text, nullable=False, index=True)
    model_id = Column(Text, nullable=False)
    state = Column(Text, nullable=False)
    sent = Column(Boolean, nullable=False, default=False)
    reserved = Column(BigInteger, nullable=False)
    charged = Column(BigInteger, nullable=True)
    pricing = Column(JSON, nullable=True)
    usage = Column(JSON, nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    settled_at = Column(BigInteger, nullable=True)
    # Normalized at settlement for the usage log and statistics (see utils/quota_usage.py).
    prompt_tokens = Column(BigInteger, nullable=True)  # all input, cache reads and writes included
    completion_tokens = Column(BigInteger, nullable=True)
    cache_read_tokens = Column(BigInteger, nullable=True)
    cache_write_tokens = Column(BigInteger, nullable=True)
    total_tokens = Column(BigInteger, nullable=True)
    cost = Column(BigInteger, nullable=True)  # micro-USD at the model price, whatever the plan unit
    __table_args__ = (
        Index('ix_quota_entry_user_created', 'user_id', 'created_at'),
        Index('ix_quota_entry_created', 'created_at'),
    )


class QuotaUsageHourly(Base):
    """Settled usage per user, model and UTC hour. Kept forever: request rows are purged."""

    __tablename__ = 'quota_usage_hourly'
    user_id = Column(Text, primary_key=True)
    model_id = Column(Text, primary_key=True)
    hour = Column(BigInteger, primary_key=True)  # epoch seconds, multiple of 3600
    requests = Column(BigInteger, nullable=False, default=0)
    prompt_tokens = Column(BigInteger, nullable=False, default=0)
    completion_tokens = Column(BigInteger, nullable=False, default=0)
    cache_read_tokens = Column(BigInteger, nullable=False, default=0)
    cache_write_tokens = Column(BigInteger, nullable=False, default=0)
    total_tokens = Column(BigInteger, nullable=False, default=0)
    cost = Column(BigInteger, nullable=False, default=0)
    __table_args__ = (Index('ix_quota_usage_hourly_hour', 'hour'),)


class QuotaAudit(Base):
    __tablename__ = 'quota_audit'
    id = Column(Text, primary_key=True)
    user_id = Column(Text, nullable=True, index=True)
    actor_id = Column(Text, nullable=False)
    action = Column(Text, nullable=False)
    data = Column(JSON, nullable=False)
    created_at = Column(BigInteger, nullable=False)


class PersonalConnection(Base):
    __tablename__ = 'personal_connection'
    id = Column(Text, primary_key=True)
    user_id = Column(Text, nullable=False, index=True)
    name = Column(Text, nullable=False)
    type = Column(Text, nullable=False)
    url = Column(Text, nullable=False)
    secret = Column(Text, nullable=False)
    config = Column(JSON, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
