"""HTTP 层测试：筛查页面用的接口能发起、查看配对与分差，权限与发布语义正确。"""

import os
import sys
from datetime import date,datetime,timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import app as app_module  # noqa: E402
from src import feed_effect_routes  # noqa: E402
from src.auth import issue  # noqa: E402
from src.database import Base,session as db_session  # noqa: E402
from src.models import User,Hive,Visit,CareAction  # noqa: E402
from src import feed_effect_models  # noqa: E402,F401  建表
from src import feed_effect_service as service  # noqa: E402

UTC=datetime


def at(day,hour=2):return datetime.combine(day,datetime.min.time())+timedelta(hours=hour)-service.SHANGHAI


@pytest.fixture()
def client():
    engine=create_engine('sqlite+pysqlite:///:memory:',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session=sessionmaker(engine,expire_on_commit=False)
    users={role:User(email=role+'@shanjing.test',password='x',role=role) for role in ['admin','beekeeper','viewer']}
    with Session() as db:
        db.add_all(users.values());db.commit()
        for user in users.values():db.refresh(user)
        _seed_material(db)
    state={'db':Session,'users':{k:v.id for k,v in users.items()}}
    queued=[]
    class FakeQueue:
        def rpush(self,key,value):queued.append((key,value))
    feed_effect_routes.queue=FakeQueue()
    def override():
        with Session() as db:yield db
    app_module.app.dependency_overrides[db_session]=override
    # auth.current 依赖 session，同样被 override 覆盖
    client=TestClient(app_module.app)
    client.state=state;client.queued=queued
    yield client
    app_module.app.dependency_overrides.clear()


def _seed_material(db):
    hive=Hive(code='H-001',site='东坡',queen_present=True,colony_frames=8,care_state='winter_ready')
    control=Hive(code='H-002',site='东坡',queen_present=True,colony_frames=8,care_state='winter_ready')
    db.add_all([hive,control]);db.flush()
    base=Visit(code='A-B',hive_id=hive.id,weight_kg=25.0,temperature=33.0,activity=60.0,feed_left=2.0,queen_seen=True,colony_frames=8,recorded_at=at(date(2026,11,3)),status='closed')
    follow=Visit(code='A-F',hive_id=hive.id,weight_kg=27.0,temperature=33.0,activity=60.0,feed_left=2.0,queen_seen=True,colony_frames=8,recorded_at=at(date(2026,11,12),20),status='closed')
    db.add_all([base,follow]);db.flush()
    db.add(CareAction(visit_id=base.id,kind='feed',feed_kg=1.5,status='closed',executed_at=at(date(2026,11,3),9),followup_weight=27.0,followup_queen=True,followup_frames=8,closed_at=at(date(2026,11,12),12)))
    db.add_all([Visit(code='C-B',hive_id=control.id,weight_kg=25.4,temperature=33.0,activity=61.0,feed_left=2.0,queen_seen=True,colony_frames=8,recorded_at=at(date(2026,11,3)),status='closed'),
                Visit(code='C-F',hive_id=control.id,weight_kg=26.0,temperature=33.0,activity=61.0,feed_left=2.0,queen_seen=True,colony_frames=8,recorded_at=at(date(2026,11,12),19),status='closed')])
    db.commit()


def token(client,role):
    return {'Authorization':'Bearer '+issue(type('U',(),{'id':client.state['users'][role],'role':role})())}


def test_create_enqueues_and_lists(client):
    response=client.post('/feed-effect-batches',json={'date_from':'2026-11-01','date_to':'2026-11-30'},headers=token(client,'beekeeper'))
    assert response.status_code==200,response.text
    batch=response.json()
    assert batch['status']=='queued'
    assert client.queued==[('apiary:feed-effect',str(batch['id']))]
    rows=client.get('/feed-effect-batches',headers=token(client,'viewer')).json()
    assert [r['id'] for r in rows]==[batch['id']]


def test_viewer_cannot_create_and_bad_range_rejected(client):
    assert client.post('/feed-effect-batches',json={'date_from':'2026-11-01','date_to':'2026-11-30'},headers=token(client,'viewer')).status_code==403
    bad=client.post('/feed-effect-batches',json={'date_from':'2026-11-30','date_to':'2026-11-01'},headers=token(client,'admin'))
    assert bad.status_code==422


def test_detail_pairing_publish_and_freeze(client):
    created=client.post('/feed-effect-batches',json={'date_from':'2026-11-01','date_to':'2026-11-30'},headers=token(client,'beekeeper')).json()
    # worker 消费：这里直接调用服务完成计算
    with client.state['db']() as db:service.compute_batch(db,created['id'])
    detail=client.get(f"/feed-effect-batches/{created['id']}",headers=token(client,'viewer')).json()
    assert detail['summary']['paired']==1
    pair=detail['pairs'][0]
    assert pair['case_hive_code']=='H-001' and pair['control_hive_code']=='H-002'
    assert pair['diff_in_diff']==pytest.approx(pair['case_delta']-pair['control_delta'],abs=1e-12)
    assert detail['missing']==[] and detail['unmatched']==[]
    # 蜂师不能发布（主管动作）
    assert client.post(f"/feed-effect-batches/{created['id']}/publish",headers=token(client,'beekeeper')).status_code==403
    published=client.post(f"/feed-effect-batches/{created['id']}/publish",headers=token(client,'admin'))
    assert published.status_code==200 and published.json()['status']=='published'
    again=client.get(f"/feed-effect-batches/{created['id']}",headers=token(client,'viewer')).json()
    assert again['snapshot']['pairs']==detail['pairs']
    # 发布后不能重试覆盖
    assert client.post(f"/feed-effect-batches/{created['id']}/retry",headers=token(client,'beekeeper')).status_code==409


def test_publish_before_compute_rejected(client):
    created=client.post('/feed-effect-batches',json={'date_from':'2026-11-01','date_to':'2026-11-30'},headers=token(client,'beekeeper')).json()
    assert client.post(f"/feed-effect-batches/{created['id']}/publish",headers=token(client,'admin')).status_code==409


def test_missing_detail_404(client):
    assert client.get('/feed-effect-batches/9999',headers=token(client,'viewer')).status_code==404
