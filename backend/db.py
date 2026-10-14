"""Local persistence; the same schema can be created on PostgreSQL via DATABASE_URL."""
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import create_engine, Column, String, Integer, JSON, ForeignKey, UniqueConstraint, event, select
from sqlalchemy.orm import declarative_base, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
STORAGE = Path(os.environ.get('TAG_STORAGE', ROOT / 'storage')).resolve()
STORAGE.mkdir(parents=True, exist_ok=True)
url = os.environ.get('DATABASE_URL', 'sqlite:///' + str(STORAGE / 'workbench.db'))
engine = create_engine(url, connect_args={'check_same_thread': False, 'timeout': 30} if url.startswith('sqlite') else {})
if url.startswith('sqlite'):
    @event.listens_for(engine, 'connect')
    def pragmas(conn, _):
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
Session = sessionmaker(engine, expire_on_commit=False)
Base = declarative_base()
def uid(): return uuid.uuid4().hex
def now(): return datetime.now(timezone.utc).isoformat()

class Record:
    id = Column(String(32), primary_key=True, default=uid)
    created_at = Column(String, default=now)

class Workspace(Record, Base):
    __tablename__ = 'workspace'
    name = Column(String, nullable=False)

class WorkspaceTrash(Record, Base):
    """Recoverable project removal marker; project data and files stay intact."""
    __tablename__ = 'workspace_trash'
    workspace_id = Column(String, ForeignKey('workspace.id'), nullable=False, unique=True, index=True)
    deleted_at = Column(String, nullable=False, default=now)

class ProjectArtifact(Record, Base):
    """Immutable, project-scoped copy/reference to a workflow artifact."""
    __tablename__ = 'project_artifact'
    workspace_id = Column(String, ForeignKey('workspace.id'), nullable=False, index=True)
    kind = Column(String, nullable=False, index=True)
    source_type = Column(String, nullable=False)
    source_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    relative_path = Column(String, nullable=False)
    sha256 = Column(String, nullable=False)
    size = Column(Integer, nullable=False, default=0)
    metadata_json = Column(JSON, nullable=False, default=dict)

class TagTemplate(Record, Base):
    __tablename__ = 'tag_template'
    workspace_id = Column(String, ForeignKey('workspace.id'), nullable=False)
    name = Column(String, nullable=False)

class TemplateRevision(Record, Base):
    __tablename__ = 'template_revision'
    template_id = Column(String, ForeignKey('tag_template.id'), nullable=False)
    revision = Column(Integer, nullable=False)
    data = Column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint('template_id','revision'),)

class CellReuse(Record, Base):
    __tablename__ = 'cell_reuse_index'
    fingerprint = Column(String, index=True, nullable=False)
    cell_id = Column(String, ForeignKey('draft_cell.id'), nullable=False, unique=True)

class Dataset(Record, Base):
    __tablename__ = 'dataset'
    workspace_id = Column(String, ForeignKey('workspace.id'), nullable=False)
    name = Column(String, nullable=False)
    version = Column(Integer, nullable=False)
    original_file = Column(String)
    source_filename = Column(String)
    sha256 = Column(String)
    sheet = Column(String)
    headers = Column(JSON)
    mapping = Column(JSON)
    warnings = Column(JSON, default=list)
    stage = Column(String, default='DATA_READY')
    __table_args__ = (UniqueConstraint('workspace_id', 'version'),)

class Product(Record, Base):
    __tablename__ = 'product'
    dataset_id = Column(String, ForeignKey('dataset.id'), nullable=False)
    row = Column(Integer, nullable=False)
    anchor = Column(String, nullable=False)
    identity_source = Column(String, default='row')
    raw = Column(JSON, nullable=False)
    fields = Column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint('dataset_id', 'row'),)

class Asset(Record, Base):
    __tablename__ = 'image_asset'
    product_id = Column(String, ForeignKey('product.id'), nullable=False)
    source = Column(String)
    path = Column(String)
    url = Column(String)
    sha256 = Column(String)
    image_type = Column(String, default='main')

class Schema(Record, Base):
    __tablename__ = 'tag_schema'
    dataset_id = Column(String, ForeignKey('dataset.id'), nullable=False)
    version = Column(Integer, nullable=False)
    goal = Column(String)
    definitions = Column(JSON, nullable=False)
    status = Column(String, default='draft')
    approved_at = Column(String)
    __table_args__ = (UniqueConstraint('dataset_id', 'version'),)

class Run(Record, Base):
    __tablename__ = 'analysis_task'
    dataset_id = Column(String, ForeignKey('dataset.id'), nullable=False)
    schema_id = Column(String, ForeignKey('tag_schema.id'), nullable=False)
    kind = Column(String, nullable=False)
    status = Column(String, default='queued')
    product_ids = Column(JSON, nullable=False)
    errors = Column(JSON, default=dict)
    model = Column(String)
    approved_at = Column(String)
    parent_id = Column(String, ForeignKey('analysis_task.id'))

class Tag(Record, Base):
    __tablename__ = 'tag_instance'
    run_id = Column(String, ForeignKey('analysis_task.id'), nullable=False)
    product_id = Column(String, ForeignKey('product.id'), nullable=False)
    values = Column(JSON, nullable=False)
    review = Column(String, default='pending')
    corrected = Column(JSON)
    review_note = Column(String)
    __table_args__ = (UniqueConstraint('run_id', 'product_id'),)

class Transform(Record, Base):
    __tablename__ = 'tag_transformation'
    run_id = Column(String, ForeignKey('analysis_task.id'), nullable=False)
    source_key = Column(String, nullable=False)
    output_key = Column(String, nullable=False)
    definition = Column(String)
    mapping = Column(JSON, nullable=False)
    type = Column(String, default='INTERPRETATION')
    status = Column(String, default='draft')
    approved_at = Column(String)

class Experience(Record, Base):
    __tablename__ = 'experience_pattern'
    workspace_id = Column(String, ForeignKey('workspace.id'), nullable=False)
    transform_id = Column(String, ForeignKey('tag_transformation.id'), nullable=False)
    problem = Column(String)
    solution = Column(JSON)

class Message(Record, Base):
    __tablename__ = 'conversation_message'
    dataset_id = Column(String, ForeignKey('dataset.id'), nullable=False)
    turn_id = Column(String, index=True)
    role = Column(String)
    text = Column(String)
    status = Column(String, default='completed')
    events = Column(JSON, default=list)
    presentation = Column(JSON, default=dict)
    error_code = Column(String)
    technical = Column(JSON, default=dict)

class ChatQuestion(Record, Base):
    __tablename__ = 'chat_question'
    dataset_id = Column(String, ForeignKey('dataset.id'), nullable=False, index=True)
    turn_id = Column(String, index=True)
    message_id = Column(String, ForeignKey('conversation_message.id'), nullable=False, index=True)
    tool_call_id = Column(String)
    question_key = Column(String)
    header = Column(String)
    prompt = Column(String, nullable=False)
    options = Column(JSON, default=list)
    status = Column(String, default='pending')
    selected_option_id = Column(String)
    selected_label = Column(String)
    answered_at = Column(String)
    error_code = Column(String)

class Observation(Record, Base):
    __tablename__ = 'image_observation'
    asset_id = Column(String, ForeignKey('image_asset.id'), nullable=False)
    model = Column(String, nullable=False)
    prompt_hash = Column(String, nullable=False)
    content = Column(JSON, nullable=False)
    usage = Column(JSON)
    __table_args__ = (UniqueConstraint('asset_id','model','prompt_hash'),)

class Audit(Record, Base):
    __tablename__ = 'audit_event'
    dataset_id = Column(String, ForeignKey('dataset.id'))
    action = Column(String)
    payload = Column(JSON)

class DraftColumn(Record, Base):
    __tablename__ = 'draft_column'
    dataset_id = Column(String, ForeignKey('dataset.id'), nullable=False)
    key = Column(String, nullable=False)
    revision = Column(Integer, default=1)
    definition = Column(JSON, nullable=False)
    origin = Column(JSON, default=dict)
    __table_args__ = (UniqueConstraint('dataset_id','key'),)

class Cell(Record, Base):
    __tablename__ = 'draft_cell'
    product_id = Column(String, ForeignKey('product.id'), nullable=False)
    column_id = Column(String, ForeignKey('draft_column.id'), nullable=False)
    revision = Column(Integer, default=1)
    edit = Column(Integer, default=0)
    status = Column(String, default='pending')
    value = Column(JSON)
    raw_value = Column(JSON)
    manual = Column(JSON)
    evidence = Column(JSON, default=dict)
    conflict = Column(JSON)
    __table_args__ = (UniqueConstraint('product_id','column_id'),)

class Proposal(Record, Base):
    __tablename__ = 'change_proposal'
    dataset_id = Column(String, ForeignKey('dataset.id'), nullable=False)
    group_id = Column(String, index=True)
    group_revision = Column(Integer, default=1)
    fingerprint = Column(String, index=True)
    origin_turn_id = Column(String, index=True)
    superseded_by = Column(String)
    confirmation_key = Column(String)
    title = Column(String)
    changes = Column(JSON)
    status = Column(String, default='pending')
    confirmed_at = Column(String)

class BatchJob(Record, Base):
    __tablename__ = 'batch_job'
    dataset_id = Column(String, ForeignKey('dataset.id'), nullable=False)
    proposal_id = Column(String, ForeignKey('change_proposal.id'))
    status = Column(String, default='queued')
    spec = Column(JSON, nullable=False)
    completed = Column(JSON, default=list)
    errors = Column(JSON, default=dict)
    metrics = Column(JSON, default=dict)

@event.listens_for(BatchJob, 'before_insert')
def stamp_job_build(mapper, connection, target):
    from .build_info import build_info
    from .execution_contract import snapshot,model_identities
    target.spec = {**(target.spec or {}), 'application_build': build_info(), 'execution_contract': {**snapshot(),'models':model_identities(target.spec or {})}}

class Snapshot(Record, Base):
    __tablename__ = 'draft_snapshot'
    dataset_id = Column(String, ForeignKey('dataset.id'), nullable=False)
    version = Column(Integer, nullable=False)
    data = Column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint('dataset_id','version'),)

class ChatSession(Record, Base):
    __tablename__ = 'dataset_chat_session'
    dataset_id = Column(String, ForeignKey('dataset.id'), unique=True, nullable=False)
    session_id = Column(String)
    model = Column(String)

class AgentRuntimeSession(Record, Base):
    """Immutable binding between one browser conversation and one native CLI session."""
    __tablename__ = 'agent_runtime_session'
    dataset_id = Column(String, ForeignKey('dataset.id'), nullable=False, index=True)
    conversation_id = Column(String, nullable=False, index=True)
    native_session_id = Column(String)
    role = Column(String, default='coordinator')
    model = Column(String)
    vision_model = Column(String)
    text_credential_id = Column(String)
    vision_credential_id = Column(String)
    fingerprint = Column(String, nullable=False, index=True)
    status = Column(String, default='active', index=True)
    invalid_reason = Column(String)
    last_used_at = Column(String, default=now)

class SourceLabelDraft(Record, Base):
    """Reviewable reverse-engineered standard which exists before a formal label column."""
    __tablename__ = 'source_label_standard_draft'
    dataset_id = Column(String, ForeignKey('dataset.id'), nullable=False, index=True)
    source_header = Column(String, nullable=False)
    revision = Column(Integer, default=1)
    status = Column(String, default='profiled', index=True)
    data = Column(JSON, nullable=False, default=dict)
    job_id = Column(String, ForeignKey('batch_job.id'))
    confirmed_column_id = Column(String, ForeignKey('draft_column.id'))

class DraftEvent(Base):
    __tablename__ = 'draft_event'
    id = Column(Integer, primary_key=True, autoincrement=True)
    dataset_id = Column(String, nullable=False, index=True)
    kind = Column(String)
    payload = Column(JSON)

class VisionCache(Record, Base):
    __tablename__ = 'draft_vision_cache'
    fingerprint = Column(String, unique=True, nullable=False)
    content = Column(JSON)

class TextRecord(Record, Base):
    __tablename__ = 'product_text_record'
    product_id = Column(String, ForeignKey('product.id'), unique=True, nullable=False)
    fingerprint = Column(String, nullable=False)
    status = Column(String, default='pending')
    content = Column(JSON, default=dict)
    source = Column(JSON, default=dict)
    provenance = Column(JSON, default=dict)
    error = Column(String)

class TextCache(Record, Base):
    __tablename__ = 'product_text_cache'
    fingerprint = Column(String, unique=True, nullable=False)
    content = Column(JSON, nullable=False)

class Migration(Base):
    __tablename__ = 'workbench_migration'
    version = Column(Integer, primary_key=True)
    applied_at = Column(String, default=now)

class WorkbenchMeta(Base):
    """Versioned additive storage for field catalogs, saved scopes and task disposition."""
    __tablename__ = 'workbench_meta'
    id = Column(String, primary_key=True)
    dataset_id = Column(String, index=True)
    kind = Column(String, index=True)
    data = Column(JSON, default=dict)
    updated_at = Column(String, default=now)

class ModelAttempt(Record, Base):
    __tablename__ = 'model_attempt'
    job_id = Column(String, index=True)
    model = Column(String)
    data = Column(JSON, default=dict)

class AgentEvent(Base):
    """Append-only agent event log. Sequence is allocated per session, never inferred from
    timestamps or rowid, so a reader can resume from after_sequence. Agent-owned: it is not
    bound to conversation, message, draft or any other business model."""
    __tablename__ = 'agent_event'
    id = Column(String(32), primary_key=True, default=uid)
    session_id = Column(String(32), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    event_type = Column(String, nullable=False, index=True)
    source = Column(String, nullable=False, default='runtime')
    payload = Column(JSON, nullable=False, default=dict)
    dataset_id = Column(String, index=True)
    created_at = Column(String, default=now)
    __table_args__ = (UniqueConstraint('session_id','sequence',name='uq_agent_event_sequence'),)

class AgentSession(Base):
    """Agent-runtime-neutral session registry for a future bridge. Deliberately has no cwd,
    instruction hash, conversation binding, session file path, migration state, presentation
    or business-question status; a reasonix session id is only a remote reference."""
    __tablename__ = 'agent_session'
    id = Column(String(32), primary_key=True, default=uid)
    dataset_id = Column(String, index=True)
    provider = Column(String)
    model = Column(String)
    status = Column(String, default='active', index=True)
    remote_session_id = Column(String)
    created_at = Column(String, default=now)
    updated_at = Column(String, default=now)
    started_at = Column(String)
    stopped_at = Column(String)

cache_engine=None
if os.environ.get('TAG_CACHE'):
    cache_path=Path(os.environ['TAG_CACHE']).resolve();cache_path.mkdir(parents=True,exist_ok=True)
    cache_engine=create_engine('sqlite:///'+str(cache_path/'cache.db'),connect_args={'check_same_thread':False,'timeout':30})
    Session.configure(binds={VisionCache:cache_engine,TextCache:cache_engine})

def init_db():
    # A consistent SQLite backup precedes the first additive schema migration.
    if engine.url.get_backend_name()=='sqlite' and engine.url.database and Path(engine.url.database).is_file():
        import sqlite3
        path=Path(engine.url.database)
        with sqlite3.connect(path) as src:
            legacy=src.execute("select 1 from sqlite_master where type='table' and name='tag_schema'").fetchone()
            migrated=src.execute("select 1 from sqlite_master where type='table' and name='draft_column'").fetchone()
            skill_migrated=src.execute("select 1 from sqlite_master where type='table' and name='product_text_record'").fetchone()
            v4=src.execute("select 1 from sqlite_master where type='table' and name='workbench_meta'").fetchone()
            v5=src.execute("select 1 from sqlite_master where type='table' and name='tag_template'").fetchone()
            product_columns={r[1] for r in src.execute("pragma table_info(product)").fetchall()} if src.execute("select 1 from sqlite_master where type='table' and name='product'").fetchone() else set()
            dataset_columns={r[1] for r in src.execute("pragma table_info(dataset)").fetchall()} if src.execute("select 1 from sqlite_master where type='table' and name='dataset'").fetchone() else set()
            v6='identity_source' in product_columns and 'source_filename' in dataset_columns
            message_columns={r[1] for r in src.execute("pragma table_info(conversation_message)").fetchall()} if src.execute("select 1 from sqlite_master where type='table' and name='conversation_message'").fetchone() else set()
            proposal_columns={r[1] for r in src.execute("pragma table_info(change_proposal)").fetchall()} if src.execute("select 1 from sqlite_master where type='table' and name='change_proposal'").fetchone() else set()
            question_table=bool(src.execute("select 1 from sqlite_master where type='table' and name='chat_question'").fetchone())
            v7=question_table and {'turn_id','presentation','error_code','technical'}<=message_columns and {'group_id','group_revision','fingerprint','origin_turn_id','superseded_by','confirmation_key'}<=proposal_columns
            v8=bool(src.execute("select 1 from sqlite_master where type='table' and name='agent_runtime_session'").fetchone()) and bool(src.execute("select 1 from sqlite_master where type='table' and name='source_label_standard_draft'").fetchone())
            if (legacy and not migrated) or (migrated and not skill_migrated) or (skill_migrated and not v4) or (v4 and not v5) or (v5 and not v6) or (v6 and not v7) or (v7 and not v8):
                folder=path.parent/'backups';folder.mkdir(exist_ok=True)
                with sqlite3.connect(folder/('before-'+('v8-' if v7 else 'v7-' if v6 else 'v6-' if v5 else 'v5-' if v4 else 'v4-' if skill_migrated else 'skill-v3-' if migrated else 'draft-')+uid()+'.db')) as dst:src.backup(dst)
    Base.metadata.create_all(engine)
    if engine.url.get_backend_name()=='sqlite':
        with engine.begin() as conn:
            columns={r[1] for r in conn.exec_driver_sql('pragma table_info(product)').fetchall()}
            if 'identity_source' not in columns:conn.exec_driver_sql("alter table product add column identity_source varchar default 'row'")
            dataset_columns={r[1] for r in conn.exec_driver_sql('pragma table_info(dataset)').fetchall()}
            if 'source_filename' not in dataset_columns:conn.exec_driver_sql('alter table dataset add column source_filename varchar')
            message_columns={r[1] for r in conn.exec_driver_sql('pragma table_info(conversation_message)').fetchall()}
            for name,kind in [('turn_id','varchar'),('presentation','json'),('error_code','varchar'),('technical','json')]:
                if name not in message_columns:conn.exec_driver_sql(f'alter table conversation_message add column {name} {kind}')
            proposal_columns={r[1] for r in conn.exec_driver_sql('pragma table_info(change_proposal)').fetchall()}
            for name,kind in [('group_id','varchar'),('group_revision','integer'),('fingerprint','varchar'),('origin_turn_id','varchar'),('superseded_by','varchar'),('confirmation_key','varchar')]:
                if name not in proposal_columns:conn.exec_driver_sql(f'alter table change_proposal add column {name} {kind}')
            conn.exec_driver_sql('create index if not exists ix_conversation_message_turn_id on conversation_message (turn_id)')
            conn.exec_driver_sql('create index if not exists ix_change_proposal_group_id on change_proposal (group_id)')
            conn.exec_driver_sql('create index if not exists ix_change_proposal_fingerprint on change_proposal (fingerprint)')
            conn.exec_driver_sql('create index if not exists ix_change_proposal_origin_turn_id on change_proposal (origin_turn_id)')
    if cache_engine is not None:Base.metadata.create_all(cache_engine,tables=[VisionCache.__table__,TextCache.__table__])
    with Session.begin() as s:
        if not s.get(Migration,3):s.add(Migration(version=3))
        if not s.get(Migration,4):s.add(Migration(version=4))
        if not s.get(Migration,5):s.add(Migration(version=5))
        if not s.get(Migration,6):
            from .identity import choose
            for product in s.scalars(select(Product)):
                product.anchor,product.identity_source=choose(product.fields,product.row)
            for dataset in s.scalars(select(Dataset)):
                if not dataset.source_filename:dataset.source_filename=dataset.name+'.xlsx'
            s.add(Migration(version=6))
        if not s.get(Migration,7):
            for proposal in s.scalars(select(Proposal)):
                if not proposal.group_id:proposal.group_id=proposal.id
                if not proposal.group_revision:proposal.group_revision=1
            for item in s.scalars(select(WorkbenchMeta).where(WorkbenchMeta.kind=='active_chat_turn')):
                s.delete(item)
            for message in s.scalars(select(Message).where(Message.status=='running')):
                message.status='interrupted'
            for question in s.scalars(select(ChatQuestion).where(ChatQuestion.status=='pending')):
                question.status='interrupted';question.error_code='reasonix_process_error'
            s.add(Migration(version=7))
        if not s.get(Migration,8):
            # Legacy native sessions were created with mutable thread metadata and
            # possibly the wrong coordinator/worker profile.  Preserve the audit
            # reference, but never resume them after this migration.
            for item in s.scalars(select(WorkbenchMeta).where(WorkbenchMeta.kind=='chat_thread')):
                native=(item.data or {}).get('session_id')
                if native:
                    s.add(AgentRuntimeSession(dataset_id=item.dataset_id,conversation_id=item.id,
                        native_session_id=native,role='coordinator',model=(item.data or {}).get('model'),
                        vision_model=(item.data or {}).get('vision_model'),fingerprint='legacy:'+item.id,
                        status='invalid',invalid_reason='legacy_profile_or_cwd'))
                data=dict(item.data or {});data.pop('session_id',None);data['active_runtime_id']=None;item.data=data
            s.add(Migration(version=8))
def obj(record): return {c.name: getattr(record, c.name) for c in record.__table__.columns}
def require(s, cls, ident):
    result = s.get(cls, ident)
    if result is None: raise ValueError(f'{cls.__name__} 不存在：{ident}')
    return result
def audit(s, dataset_id, action, payload): s.add(Audit(dataset_id=dataset_id, action=action, payload=payload))
