"""饲喂效果对照配对的离线测试：不依赖 PostgreSQL/Redis，直接用 SQLite 内存库。

覆盖：区间选取、同一参考重算、对照箱资格、对照箱每批只用一次、
先最大化配对数量再最小化配对前分值差、缺测与未配上的区分、
材料指纹与发布前重配、发布后冻结依据、续跑不重复发布。
"""

import os
import sys
from datetime import date,datetime,timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import Base  # noqa: E402
from src.models import User,Hive,Visit,CareAction  # noqa: E402
from src.season_models import ApiaryMove  # noqa: E402
from src import feed_effect_models  # noqa: E402,F401  建表
from src import feed_effect_service as service  # noqa: E402
from src.feed_effect_models import FeedEffectBatch,FeedEffectCase,FeedEffectPair  # noqa: E402

UTC=datetime


@pytest.fixture()
def db():
    engine=create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session=sessionmaker(engine,expire_on_commit=False)
    with Session() as session:
        session.add(User(email='admin@shanjing.test',password='x',role='admin'))
        session.commit()
        yield session


def at(day,hour=2):
    """上海时间某天 hour 点对应的 UTC naive 时间（库里存 UTC）。"""
    return datetime.combine(day,datetime.min.time())+timedelta(hours=hour)-service.SHANGHAI


def make_hive(db,code,site='东坡'):
    hive=Hive(code=code,site=site,queen_present=True,colony_frames=8,care_state='winter_ready')
    db.add(hive);db.flush();return hive


def make_visit(db,hive,code,day,hour=2,weight=25.0,temperature=33.0,activity=60.0,feed_left=2.0):
    visit=Visit(code=code,hive_id=hive.id,weight_kg=weight,temperature=temperature,activity=activity,
                feed_left=feed_left,queen_seen=True,colony_frames=8,recorded_at=at(day,hour),status='closed')
    db.add(visit);db.flush();return visit


def make_case(db,hive,code_prefix,day,closed_day,weight=25.0,weight_after=27.0):
    base=make_visit(db,hive,code_prefix+'-B',day,weight=weight)
    follow=make_visit(db,hive,code_prefix+'-F',closed_day,hour=20,weight=weight_after)
    care=CareAction(visit_id=base.id,kind='feed',feed_kg=1.5,status='closed',
                    executed_at=at(day,10),followup_weight=weight_after,followup_queen=True,followup_frames=8,
                    closed_at=at(closed_day,12))
    db.add(care);db.flush();return care,base,follow


def hex_weights():
    """取四组落在参考分布内的读数，分值相近，便于制造配对竞争。"""
    return [dict(weight=26.0,temperature=33.0,activity=55.0,feed_left=2.0),
            dict(weight=26.2,temperature=33.1,activity=56.0,feed_left=2.1),
            dict(weight=30.0,temperature=33.2,activity=70.0,feed_left=2.4),
            dict(weight=30.1,temperature=33.3,activity=71.0,feed_left=2.5)]


def batch(db,date_from=date(2026,11,1),date_to=date(2026,11,30),create_user=None):
    user=create_user or db.query(User).first()
    row=service.create(db,type('B',(),{'date_from':date_from,'date_to':date_to})(),user)
    return row


def compute(db,row):
    result=service.compute_batch(db,row.id)
    assert result['status']=='computed',result
    return result


def test_range_and_recomputed_delta_uses_single_reference(db):
    hive=make_hive(db,'H-001');control=make_hive(db,'H-002')
    care,base,follow=make_case(db,hive,'H-001',date(2026,11,3),date(2026,11,12))
    make_visit(db,control,'C-B',date(2026,11,3),weight=24.0)
    make_visit(db,control,'C-F',date(2026,11,12),hour=19,weight=26.5)
    outside,_,_=make_case(db,hive,'H-001b',date(2026,11,3),date(2026,12,5))
    db.commit()
    row=batch(db);compute(db,row)
    detail=service.detail(db,row)
    assert len(detail['cases'])==1,detail['cases']
    case=detail['cases'][0]
    assert case['care_id']==care.id
    assert case['state']=='paired'
    model,_=service.reference_model()
    expected_base=float(model.score_samples([[getattr(base,k) for k in service.FEATURES]])[0])
    expected_follow=float(model.score_samples([[getattr(follow,k) for k in service.FEATURES]])[0])
    assert case['baseline_score']==pytest.approx(expected_base,abs=1e-12)
    assert case['delta']==pytest.approx(expected_follow-expected_base,abs=1e-12)
    pair=detail['pairs'][0]
    assert pair['control_hive_code']=='H-002'
    assert pair['diff_in_diff']==pytest.approx(case['delta']-pair['control_delta'],abs=1e-12)
    assert db.query(FeedEffectCase).filter_by(batch_id=row.id,care_id=outside.id).count()==0


def test_control_requires_both_days_and_no_move_or_care(db):
    hive=make_hive(db,'H-001')
    good=make_hive(db,'H-002')
    one_day=make_hive(db,'H-003');moved=make_hive(db,'H-004');cared=make_hive(db,'H-005');other_site=make_hive(db,'H-006','林缘')
    make_case(db,hive,'H-001',date(2026,11,3),date(2026,11,12))
    make_visit(db,good,'G-B',date(2026,11,3),weight=25.0);make_visit(db,good,'G-F',date(2026,11,12),hour=18,weight=26.0)
    make_visit(db,one_day,'O-B',date(2026,11,3),weight=24.5)
    make_visit(db,moved,'M-B',date(2026,11,3),weight=25.1);make_visit(db,moved,'M-F',date(2026,11,12),hour=18,weight=26.1)
    make_visit(db,cared,'C-B',date(2026,11,3),weight=25.2);make_visit(db,cared,'C-F',date(2026,11,12),hour=18,weight=26.2)
    make_visit(db,other_site,'S-B',date(2026,11,3),weight=25.3);make_visit(db,other_site,'S-F',date(2026,11,12),hour=18,weight=26.3)
    db.add(ApiaryMove(hive_id=moved.id,destination='南麓',move_date=date(2026,11,7),health_note='检疫合格',origin_snapshot={'site':'东坡'},state='moved'))
    blocked_base=make_visit(db,cared,'C2-B',date(2026,11,5),weight=25.0)
    db.add(CareAction(visit_id=blocked_base.id,kind='feed',feed_kg=1.0,status='executed',executed_at=at(date(2026,11,5),9)))
    db.add(ApiaryMove(hive_id=moved.id,destination='南麓2',move_date=date(2026,11,10),health_note='取消不阻断',origin_snapshot={'site':'东坡'},state='cancelled'))
    db.commit()
    row=batch(db);compute(db,row)
    pair=service.detail(db,row)['pairs'][0]
    assert pair['control_hive_code']=='H-002'


def test_control_only_once_and_maximise_pairs_first(db):
    # A 的窗口 11-03→11-12，B 的窗口 11-05→11-15。X 只在 A 窗口有两天读数，Y 两个窗口都有。
    # 贪心把 Y 先给 A 会让 B 无对照；正确解必须 A→X、B→Y，配成两单。
    a=make_hive(db,'H-010');b=make_hive(db,'H-011');x=make_hive(db,'H-012');y=make_hive(db,'H-013')
    make_case(db,a,'A',date(2026,11,3),date(2026,11,12),weight=25.0)
    make_case(db,b,'B',date(2026,11,5),date(2026,11,15),weight=25.3)
    make_visit(db,x,'X-B',date(2026,11,3),weight=25.0);make_visit(db,x,'X-F',date(2026,11,12),hour=18,weight=26.2)
    make_visit(db,y,'Y-B',date(2026,11,3),weight=25.0);make_visit(db,y,'Y-F',date(2026,11,12),hour=18,weight=26.0)
    make_visit(db,y,'Y-B2',date(2026,11,5),weight=25.1);make_visit(db,y,'Y-F2',date(2026,11,15),hour=18,weight=26.4)
    db.commit()
    row=batch(db);compute(db,row)
    detail=service.detail(db,row)
    assert len(detail['pairs'])==2
    control_of={p['case_hive_code']:p['control_hive_code'] for p in detail['pairs']}
    assert control_of=={'H-010':'H-012','H-011':'H-013'}


def test_closeness_breaks_ties_between_matchings_of_same_size(db):
    # 两单 A、B 都只能从同一对对照 X、Z 中挑；两种完整配对都可能。
    # 建单读数：A 比 X 近、B 比 Z 近（交叉配对总分差更大），必须选中总分差更小的直配。
    a=make_hive(db,'H-020');b=make_hive(db,'H-021')
    x=make_hive(db,'H-022');z=make_hive(db,'H-023')
    make_case(db,a,'A',date(2026,11,3),date(2026,11,12),weight=25.0)
    make_case(db,b,'B',date(2026,11,3),date(2026,11,12),weight=32.0)
    make_visit(db,x,'X-B',date(2026,11,3),weight=25.1);make_visit(db,x,'X-F',date(2026,11,12),hour=18,weight=25.8)
    make_visit(db,z,'Z-B',date(2026,11,3),weight=31.9);make_visit(db,z,'Z-F',date(2026,11,12),hour=18,weight=32.6)
    db.commit()
    row=batch(db);compute(db,row)
    detail=service.detail(db,row)
    assert len(detail['pairs'])==2
    control_of={p['case_hive_code']:p['control_hive_code'] for p in detail['pairs']}
    assert control_of=={'H-020':'H-022','H-021':'H-023'}


def test_matching_is_optimal_against_brute_force(db):
    """小规模随机图：与全排列暴力解比较，先比配对数再比配对前分值差总和。"""
    import itertools,random
    random.seed(7)
    for trial in range(25):
        db.query(FeedEffectPair).delete();db.query(FeedEffectCase).delete();db.query(FeedEffectBatch).delete()
        db.query(CareAction).delete();db.query(Visit).delete();db.query(Hive).delete()
        db.commit();db.expire_all()
        n_cases=random.randint(1,4);n_controls=random.randint(1,4)
        case_hives=[make_hive(db,f'C{i}-{trial}') for i in range(n_cases)]
        control_hives=[make_hive(db,f'K{j}-{trial}') for j in range(n_controls)]
        for i,hive in enumerate(case_hives):
            make_case(db,hive,f'W{i}-{trial}',date(2026,11,3),date(2026,11,12),weight=random.uniform(16,33))
        for j,hive in enumerate(control_hives):
            weight=random.uniform(16,33)
            make_visit(db,hive,f'K{j}b-{trial}',date(2026,11,3),weight=weight)
            make_visit(db,hive,f'K{j}f-{trial}',date(2026,11,12),hour=18,weight=random.uniform(16,33))
        db.commit()
        row=batch(db);compute(db,row)
        detail=service.detail(db,row)
        model,_=service.reference_model()
        # 用服务内部候选与分数，独立暴力求最优
        rows=service._load_cases(db,row.date_from,row.date_to)
        ctx=service._context(db,rows)
        scores={}
        cases=service._build_cases(rows,ctx,scores)
        options=[c['candidates'] for c in cases]
        best_pairs=0;best_gap=None
        # 暴力：给每张单挑一个对照或不挑，校验对照不重复
        for choice in itertools.product(*[[None]+list(range(len(opts))) for opts in options]):
            used=[o['hive_id'] for o,idx in zip(options,choice) if idx is not None for o in [o[idx]]]
            if len(used)!=len(set(used)):continue
            count=sum(1 for c in choice if c is not None)
            gap=sum(options[i][idx]['gap'] for i,idx in enumerate(choice) if idx is not None)
            if count>best_pairs or (count==best_pairs and (best_gap is None or gap<best_gap-1e-12)):
                best_pairs=count;best_gap=gap
        assert len(detail['pairs'])==best_pairs,(trial,n_cases,n_controls)
        if best_pairs:
            assert sum(p['baseline_gap'] for p in detail['pairs'])==pytest.approx(best_gap,abs=1e-9),(trial,best_gap)



def test_missing_and_unmatched_are_separated(db):
    no_follow=make_hive(db,'H-030');alone=make_hive(db,'H-031');lonely=make_hive(db,'H-032')
    no_follow_care,_,_=make_case(db,no_follow,'N',date(2026,11,3),date(2026,11,12))
    db.query(Visit).filter_by(code='N-F').delete()
    make_case(db,alone,'A',date(2026,11,3),date(2026,11,12))
    make_visit(db,lonely,'L-B',date(2026,11,4),weight=25.0)
    db.commit()
    row=batch(db);compute(db,row)
    detail=service.detail(db,row)
    assert [c['care_id'] for c in detail['missing']]==[no_follow_care.id]
    assert detail['missing'][0]['reason']=='结案后尚无巡检读数'
    assert [c['care_id'] for c in detail['unmatched']]
    assert detail['summary']['missing']==1
    assert detail['summary']['unmatched']==1
    assert detail['summary']['paired']==0


def test_publish_repairs_when_material_changed_then_freezes(db):
    hive=make_hive(db,'H-040');control=make_hive(db,'H-041')
    make_case(db,hive,'A',date(2026,11,3),date(2026,11,12))
    make_visit(db,control,'C-B',date(2026,11,3),weight=25.0)
    db.commit()
    row=batch(db);compute(db,row)
    assert service.detail(db,row)['summary']['unmatched']==1
    # 补巡检：对照箱结案后读数补录，材料变化
    make_visit(db,control,'C-F',date(2026,11,12),hour=18,weight=26.0)
    db.commit()
    user=db.query(User).first()
    published,repaired=service.publish(db,row.id,user)
    assert repaired is True
    assert published.status=='published'
    assert published.revisions==1
    assert published.summary['paired']==1
    snapshot=published.snapshot
    # 再补一条更接近的单，发布后的依据仍保持不变
    extra=make_hive(db,'H-042');make_case(db,extra,'E',date(2026,11,3),date(2026,11,12))
    make_visit(db,make_hive(db,'H-043'),'F-B',date(2026,11,3),weight=25.0)
    make_visit(db,db.query(Hive).filter_by(code='H-043').one(),'F-F',date(2026,11,12),hour=18,weight=26.5)
    db.commit()
    frozen=service.frozen_detail(service.publish(db,row.id,user)[0])
    assert frozen['snapshot']['pairs']==snapshot['pairs']
    assert len(frozen['cases'])==1
    assert len(service.detail(db,row)['cases'])==1  # 当前表未被后续补录改写


def test_resume_is_idempotent_and_new_batches_can_recompute(db):
    hive=make_hive(db,'H-050');control=make_hive(db,'H-051')
    make_case(db,hive,'A',date(2026,11,3),date(2026,11,12))
    make_visit(db,control,'C-B',date(2026,11,3),weight=25.0)
    make_visit(db,control,'C-F',date(2026,11,12),hour=18,weight=26.0)
    db.commit()
    row=batch(db)
    first=service.compute_batch(db,row.id)
    second=service.compute_batch(db,row.id)
    assert first['status']=='computed'
    assert second.get('skipped') is True
    assert db.query(FeedEffectPair).filter_by(batch_id=row.id).count()==1
    db.refresh(row)
    assert row.status=='computed' and row.attempts==1
    queued=batch(db)
    assert queued.id in service.resume_ids(db)
    service.compute_batch(db,queued.id)
    assert queued.id not in service.resume_ids(db)


def test_publish_twice_does_not_duplicate_or_revise(db):
    hive=make_hive(db,'H-060');control=make_hive(db,'H-061')
    make_case(db,hive,'A',date(2026,11,3),date(2026,11,12))
    make_visit(db,control,'C-B',date(2026,11,3),weight=25.0)
    make_visit(db,control,'C-F',date(2026,11,12),hour=18,weight=26.0)
    db.commit();row=batch(db);compute(db,row)
    user=db.query(User).first()
    first,repaired1=service.publish(db,row.id,user)
    second,repaired2=service.publish(db,row.id,user)
    assert repaired1 is False and repaired2 is False
    assert first.revisions==0 and second.revisions==0
    assert first.snapshot==second.snapshot
    assert db.query(FeedEffectPair).filter_by(batch_id=row.id).count()==1
