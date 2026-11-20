from datetime import date,datetime
from sqlalchemy import String,Float,Date,DateTime,ForeignKey,JSON,UniqueConstraint,func
from sqlalchemy.orm import Mapped,mapped_column
from .database import Base


class FeedEffectBatch(Base):
    """一次按结案日期挑选饲喂单后发起的对照配对批次。"""
    __tablename__='feed_effect_batches'
    id:Mapped[int]=mapped_column(primary_key=True)
    date_from:Mapped[date]=mapped_column(Date)
    date_to:Mapped[date]=mapped_column(Date)
    status:Mapped[str]=mapped_column(String(24),default='queued',index=True)
    reference_sha:Mapped[str]=mapped_column(String(64),default='')
    threshold:Mapped[float|None]=mapped_column(Float,nullable=True)
    fingerprint:Mapped[str]=mapped_column(String(64),default='')
    revisions:Mapped[int]=mapped_column(default=0)
    attempts:Mapped[int]=mapped_column(default=0)
    error:Mapped[str]=mapped_column(String(500),default='')
    summary:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    snapshot:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    created_by:Mapped[str]=mapped_column(String(120),default='')
    created_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now())
    started_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    published_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)


class FeedEffectCase(Base):
    """批次内一张饲喂单：建单巡检、结案后首份巡检及重算分值。"""
    __tablename__='feed_effect_cases'
    __table_args__=(UniqueConstraint('batch_id','care_id'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    batch_id:Mapped[int]=mapped_column(ForeignKey('feed_effect_batches.id'),index=True)
    care_id:Mapped[int]=mapped_column(ForeignKey('care_actions.id'))
    hive_id:Mapped[int]=mapped_column(ForeignKey('hives.id'))
    visit_id:Mapped[int]=mapped_column(ForeignKey('visits.id'))
    followup_visit_id:Mapped[int|None]=mapped_column(ForeignKey('visits.id'),nullable=True)
    state:Mapped[str]=mapped_column(String(20),default='missing')
    reason:Mapped[str]=mapped_column(String(300),default='')
    baseline_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    followup_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    baseline_weight:Mapped[float|None]=mapped_column(Float,nullable=True)
    followup_weight:Mapped[float|None]=mapped_column(Float,nullable=True)
    baseline_score:Mapped[float|None]=mapped_column(Float,nullable=True)
    followup_score:Mapped[float|None]=mapped_column(Float,nullable=True)
    delta:Mapped[float|None]=mapped_column(Float,nullable=True)
    created_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now())


class FeedEffectPair(Base):
    """一张饲喂单与同场对照箱的配对；每批对照箱只用一回。"""
    __tablename__='feed_effect_pairs'
    __table_args__=(UniqueConstraint('batch_id','case_id'),UniqueConstraint('batch_id','control_hive_id'))
    id:Mapped[int]=mapped_column(primary_key=True)
    batch_id:Mapped[int]=mapped_column(ForeignKey('feed_effect_batches.id'),index=True)
    case_id:Mapped[int]=mapped_column(ForeignKey('feed_effect_cases.id'))
    control_hive_id:Mapped[int]=mapped_column(ForeignKey('hives.id'))
    control_baseline_visit_id:Mapped[int]=mapped_column(ForeignKey('visits.id'))
    control_followup_visit_id:Mapped[int]=mapped_column(ForeignKey('visits.id'))
    control_baseline_score:Mapped[float]=mapped_column(Float)
    control_followup_score:Mapped[float]=mapped_column(Float)
    control_delta:Mapped[float]=mapped_column(Float)
    case_delta:Mapped[float]=mapped_column(Float)
    diff_in_diff:Mapped[float]=mapped_column(Float)
    baseline_gap:Mapped[float]=mapped_column(Float)
    created_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now())
