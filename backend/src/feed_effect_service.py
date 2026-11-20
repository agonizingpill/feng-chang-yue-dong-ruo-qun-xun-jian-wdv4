"""饲喂效果对照配对：按结案日期选饲喂单，按同一参考重算分差，再找同场对照箱。

口径
- 饲喂单：kind=feed、status=closed，且 closed_at 的上海日历日期落在所选区间。
- 建单巡检：CareAction.visit_id 指向的巡检；结案后首份巡检：该箱 recorded_at 严格晚于
  closed_at 的最早一条巡检。缺任一份读数记为 missing。
- 分差：两份巡检都用 backend/fixtures/winter_reference.json 的同一个 IsolationForest
  重新计算 score_samples，delta = 结案后分值 − 建单分值，不采用巡检上已存的分值。
- 对照箱：与饲喂箱同场（当前 site 相同）、在建单日与结案后巡检日都有读数、区间内没有
  非取消转场、没有非取消处置与该区间重叠，且不是本批另一张饲喂单的蜂箱。
- 两箱分差之差：(饲喂箱结案后分值−建单分值) − (对照箱后读分值−前读分值)。
- 配对：对照箱每批只用一次；先最大化配成单数，再最小化配对前（建单巡检）分值差。
"""

import hashlib
import json
from collections import defaultdict,deque
from datetime import datetime,time,timedelta

from fastapi import HTTPException
from sqlalchemy import delete,select

from .feed_effect_models import FeedEffectBatch,FeedEffectCase,FeedEffectPair
from .models import CareAction,Hive,Visit
from .season_models import ApiaryMove
from .screen_model import FEATURES,reference_model
from .yard_service import audit

SHANGHAI=timedelta(hours=8)
BONUS=10**24          # 每配成一单的固定奖励，远大于任何分值差总和
GAP_QUANTUM=10**9     # 分值差量化到 1e-9
GAP_WEIGHT=10**9      # 量化后的分值差远重于并列项（并列项上限约 80×80×80）
STALE_SECONDS=120     # 中断后重新认领的等待时间


def sh_date(value):
    return None if value is None else (value+SHANGHAI).date()


def _iso(value):
    return None if value is None else value.isoformat()


def _day_start_utc(day):
    return datetime.combine(day,time.min)-SHANGHAI


def _day_end_utc(day):
    return datetime.combine(day+timedelta(days=1),time.min)-SHANGHAI-timedelta(microseconds=1)


def _score(visit,cache):
    if visit.id not in cache:
        model,_=reference_model()
        cache[visit.id]=float(model.score_samples([[getattr(visit,k) for k in FEATURES]])[0])
    return cache[visit.id]


def _visit_payload(visit):
    if not visit:return None
    return {'id':visit.id,'hive_id':visit.hive_id,'code':visit.code,'recorded_at':_iso(visit.recorded_at),
            **{k:getattr(visit,k) for k in FEATURES}}


def _load_cases(db,date_from,date_to):
    cares=db.scalars(select(CareAction).where(CareAction.kind=='feed',CareAction.status=='closed',CareAction.closed_at.isnot(None)).order_by(CareAction.id)).all()
    rows=[]
    for care in cares:
        if not(date_from<=sh_date(care.closed_at)<=date_to):continue
        visit=db.get(Visit,care.visit_id)
        if not visit:continue
        hive=db.get(Hive,visit.hive_id)
        if not hive:continue
        followup=db.scalar(select(Visit).where(Visit.hive_id==hive.id,Visit.id!=visit.id,Visit.recorded_at>care.closed_at).order_by(Visit.recorded_at.asc(),Visit.id.asc()))
        rows.append((care,hive,visit,followup))
    return rows


def _context(db,rows):
    hives=db.scalars(select(Hive).order_by(Hive.id)).all()
    visits=db.scalars(select(Visit).order_by(Visit.id)).all()
    cares=db.scalars(select(CareAction).order_by(CareAction.id)).all()
    moves=db.scalars(select(ApiaryMove).order_by(ApiaryMove.id)).all()
    sites={h.site for _,h,_,_ in rows}
    relevant_ids={h.id for h in hives if h.site in sites}
    visit_hive={v.id:v.hive_id for v in visits}
    by_hive=defaultdict(list)
    for v in visits:
        if v.hive_id in relevant_ids:by_hive[v.hive_id].append(v)
    for items in by_hive.values():items.sort(key=lambda v:(v.recorded_at,v.id))
    return {'hives':hives,'visits_by_hive':by_hive,'visit_by_id':{v.id:v for v in visits},'visit_hive':visit_hive,
            'cares':cares,'moves':moves,
            'relevant_visits':[v for v in visits if v.hive_id in relevant_ids],
            'relevant_cares':[c for c in cares if visit_hive.get(c.visit_id) in relevant_ids],
            'relevant_moves':[m for m in moves if m.hive_id in relevant_ids]}


def _fingerprint_payload(rows,ctx):
    sites={hive.site for _,hive,_,_ in rows}
    return {
        'reference_sha':reference_model()[1]['sha256'],
        'cases':[{'care_id':care.id,'kind':care.kind,'status':care.status,'closed_at':_iso(care.closed_at),
                  'hive':{'id':hive.id,'code':hive.code,'site':hive.site},
                  'baseline':_visit_payload(base),'followup':_visit_payload(fup)} for care,hive,base,fup in rows],
        'hives':[{'id':h.id,'code':h.code,'site':h.site} for h in ctx['hives'] if h.site in sites],
        'visits':[_visit_payload(v) for v in ctx['relevant_visits']],
        'cares':[{'id':c.id,'visit_id':c.visit_id,'kind':c.kind,'status':c.status,
                  'executed_at':_iso(c.executed_at),'closed_at':_iso(c.closed_at)} for c in ctx['relevant_cares']],
        'moves':[{'id':m.id,'hive_id':m.hive_id,'state':m.state,'move_date':m.move_date.isoformat(),
                  'destination':m.destination} for m in ctx['relevant_moves']],
    }


def _fingerprint(rows,ctx):
    raw=json.dumps(_fingerprint_payload(rows,ctx),ensure_ascii=False,separators=(',',':'),sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def current_fingerprint(db,date_from,date_to):
    rows=_load_cases(db,date_from,date_to)
    return _fingerprint(rows,_context(db,rows))


def _pick(visits,day,reference,exclude=None):
    options=[v for v in visits if v.id!=exclude and sh_date(v.recorded_at)==day]
    if not options:return None
    return min(options,key=lambda v:(abs((v.recorded_at-reference).total_seconds()),v.id))


def _move_blocks(moves,hive_id,first_day,last_day):
    return any(m.hive_id==hive_id and m.state!='cancelled' and first_day<=m.move_date<=last_day for m in moves)


def _care_blocks(ctx,hive_id,window_start,window_end):
    for care in ctx['cares']:
        if care.status=='cancelled' or ctx['visit_hive'].get(care.visit_id)!=hive_id:continue
        source=ctx['visit_by_id'].get(care.visit_id)
        if not source:continue
        if source.recorded_at<=window_end and (care.closed_at is None or care.closed_at>=window_start):return True
    return False


def _candidates(case,ctx,scores,treated):
    """候选对照箱按建单分值差升序；treated 是本批饲喂箱，不能当对照。"""
    first_day,last_day=case['baseline_day'],case['followup_day']
    window_start=_day_start_utc(first_day);window_end=_day_end_utc(last_day)
    options=[]
    for hive in ctx['hives']:
        if hive.site!=case['site'] or hive.id==case['hive_id'] or hive.id in treated:continue
        visits=ctx['visits_by_hive'].get(hive.id,[])
        base=_pick(visits,first_day,case['baseline_at'])
        if not base:continue
        fup=_pick(visits,last_day,case['followup_at'],exclude=base.id)
        if not fup:continue
        if _move_blocks(ctx['moves'],hive.id,first_day,last_day):continue
        if _care_blocks(ctx,hive.id,window_start,window_end):continue
        base_score=_score(base,scores);fup_score=_score(fup,scores)
        options.append({'hive_id':hive.id,'hive_code':hive.code,'baseline':base,'followup':fup,
                        'baseline_score':base_score,'followup_score':fup_score,'delta':fup_score-base_score,
                        'gap':abs(base_score-case['baseline_score'])})
    options.sort(key=lambda o:(o['gap'],o['hive_id']))
    return options


class _Flow:
    """最小费用流：SPFA 找最短增广路，只在边际费用为负时继续增广。"""
    def __init__(self,n):self.n=n;self.graph=[[] for _ in range(n)]

    def edge(self,u,v,capacity,cost):
        forward=[v,capacity,cost,len(self.graph[v])]
        backward=[u,0,-cost,len(self.graph[u])]
        self.graph[u].append(forward);self.graph[v].append(backward)
        return forward

    def run(self,source,sink):
        while True:
            distance=[None]*self.n;previous=[None]*self.n;queued=[False]*self.n
            distance[source]=0;queue=deque([source]);queued[source]=True
            while queue:
                u=queue.popleft();queued[u]=False
                for i,edge in enumerate(self.graph[u]):
                    v,capacity,cost,_=edge
                    if capacity>0 and (distance[v] is None or distance[u]+cost<distance[v]):
                        distance[v]=distance[u]+cost;previous[v]=(u,i)
                        if not queued[v]:queue.append(v);queued[v]=True
            if distance[sink] is None or distance[sink]>=0:return
            node=sink
            while node!=source:
                u,i=previous[node];edge=self.graph[u][i]
                edge[1]-=1;self.graph[node][edge[3]][1]+=1;node=u


def _match_candidates(options):
    """返回每个单选中的对照箱 id 或 None。

    源→单→对照箱→汇；配成边带固定奖励，故先最大化配成单数；其余费用为建单
    分值差（量化到 1e-9）与确定性并列项，故再最小化配对前分值差。
    """
    count=len(options)
    keys=sorted({o['hive_id'] for opts in options for o in opts})
    index={key:j for j,key in enumerate(keys)}
    source=0;sink=1+count+len(keys)
    flow=_Flow(sink+1);tagged=[]
    tie_stride=max(1,len(keys)*max((len(opts) for opts in options),default=1))
    for i,opts in enumerate(options):
        flow.edge(source,1+i,1,0)
        for option in opts:
            gap=int(round(option['gap']*GAP_QUANTUM))*GAP_WEIGHT
            cost=-BONUS+gap+i*tie_stride+index[option['hive_id']]
            edge=flow.edge(1+i,1+count+index[option['hive_id']],1,cost)
            tagged.append((i,edge,option['hive_id']))
    for j in range(len(keys)):flow.edge(1+count+j,sink,1,0)
    flow.run(source,sink)
    chosen=[None]*count
    for i,edge,hive_id in tagged:
        if edge[1]==0 and chosen[i] is None:chosen[i]=hive_id
    return chosen


def _build_cases(rows,ctx,scores):
    treated={hive.id for _,hive,_,_ in rows}
    cases=[]
    for care,hive,base,fup in rows:
        case={'care':care,'care_id':care.id,'hive_id':hive.id,'site':hive.site,'hive_code':hive.code,
              'baseline':base,'followup':fup,'baseline_at':base.recorded_at if base else None,
              'followup_at':fup.recorded_at if fup else None,'baseline_score':None,'followup_score':None,
              'delta':None,'state':'missing','reason':'','candidates':[]}
        if not base:
            case['reason']='建单巡检缺失'
        elif not fup:
            case['reason']='结案后尚无巡检读数'
        else:
            case['baseline_day']=sh_date(base.recorded_at);case['followup_day']=sh_date(fup.recorded_at)
            case['baseline_score']=_score(base,scores);case['followup_score']=_score(fup,scores)
            case['delta']=case['followup_score']-case['baseline_score']
            case['candidates']=_candidates(case,ctx,scores,treated)
            if case['candidates']:case['state']='candidate'
            else:case.update(state='unmatched',reason='同场无两天都有读数且期间无转场或处置的对照箱')
        cases.append(case)
    return cases


def _compute(db,batch):
    rows=_load_cases(db,batch.date_from,batch.date_to)
    ctx=_context(db,rows)
    fingerprint=_fingerprint(rows,ctx)
    _,evidence=reference_model()
    scores={}
    cases=_build_cases(rows,ctx,scores)
    matchable=[c for c in cases if c['state']=='candidate']
    chosen=_match_candidates([c['candidates'] for c in matchable])
    pairs=[]
    for case,control_id in zip(matchable,chosen):
        if control_id is None:
            case.update(state='unmatched',reason='同场对照箱已被本批其他单占用')
            continue
        control=next(o for o in case['candidates'] if o['hive_id']==control_id)
        case['state']='paired';pairs.append((case,control))
    db.execute(delete(FeedEffectPair).where(FeedEffectPair.batch_id==batch.id))
    db.execute(delete(FeedEffectCase).where(FeedEffectCase.batch_id==batch.id))
    db.flush()
    stored={}
    for case in cases:
        row=FeedEffectCase(batch_id=batch.id,care_id=case['care_id'],hive_id=case['hive_id'],visit_id=case['baseline'].id,
                           followup_visit_id=case['followup'].id if case['followup'] else None,state=case['state'],
                           reason=case['reason'],baseline_at=case['baseline_at'],followup_at=case['followup_at'],
                           baseline_weight=case['baseline'].weight_kg if case['baseline'] else None,
                           followup_weight=case['followup'].weight_kg if case['followup'] else None,
                           baseline_score=case['baseline_score'],followup_score=case['followup_score'],delta=case['delta'])
        db.add(row);stored[case['care_id']]=row
    db.flush()
    for case,control in pairs:
        db.add(FeedEffectPair(batch_id=batch.id,case_id=stored[case['care_id']].id,control_hive_id=control['hive_id'],
                              control_baseline_visit_id=control['baseline'].id,control_followup_visit_id=control['followup'].id,
                              control_baseline_score=control['baseline_score'],control_followup_score=control['followup_score'],
                              control_delta=control['delta'],case_delta=case['delta'],
                              diff_in_diff=case['delta']-control['delta'],baseline_gap=control['gap']))
    diffs=[case['delta']-control['delta'] for case,control in pairs]
    summary={'selected':len(cases),'paired':len(pairs),
             'unmatched':sum(1 for c in cases if c['state']=='unmatched'),
             'missing':sum(1 for c in cases if c['state']=='missing'),
             'controls_used':len({control['hive_id'] for _,control in pairs}),
             'controls_considered':len({o['hive_id'] for c in matchable for o in c['candidates']}),
             'mean_diff':round(sum(diffs)/len(diffs),6) if diffs else None}
    batch.fingerprint=fingerprint;batch.reference_sha=evidence['sha256'];batch.threshold=evidence['threshold']
    batch.summary=summary;batch.status='computed';batch.error=''
    return {'status':'computed','batch_id':batch.id,'summary':summary}


def compute_batch(db,batch_id):
    """计算一个批次。整段计算在一笔持有行锁的事务内完成：

    - 崩溃/中断时事务回滚，批次仍留在 queued，worker 续跑原批；
    - 与补巡检/补处置并发时，计算基于其开始时的材料，发布时再按指纹核对；
    - 已发布批次不再重算，避免覆盖当时依据；重复入队也不会重复计算。
    """
    batch=db.scalar(select(FeedEffectBatch).where(FeedEffectBatch.id==batch_id).with_for_update().execution_options(populate_existing=True))
    if not batch:return {'status':'absent','batch_id':batch_id}
    if batch.status=='published':return {'status':'published','batch_id':batch.id,'skipped':True}
    if batch.status=='computed':return {'status':'computed','batch_id':batch.id,'skipped':True}
    if batch.status=='computing' and batch.started_at and (datetime.utcnow()-batch.started_at).total_seconds()<STALE_SECONDS:
        return {'status':'running','batch_id':batch.id,'skipped':True}
    batch.status='computing';batch.started_at=datetime.utcnow();batch.attempts=(batch.attempts or 0)+1;batch.error=''
    db.flush()
    try:
        result=_compute(db,batch)
        db.commit();return result
    except Exception as error:
        db.rollback()
        failed=db.get(FeedEffectBatch,batch_id)
        if failed and failed.status!='published':
            failed.status='failed';failed.error=str(error)[:500];db.commit()
        return {'status':'failed','batch_id':batch_id,'error':str(error)}


def resume_ids(db):
    """排队中或计算中断的批次，供worker续跑原批。"""
    batches=db.scalars(select(FeedEffectBatch).where(FeedEffectBatch.status.in_(['queued','computing'])).order_by(FeedEffectBatch.id)).all()
    now=datetime.utcnow()
    return [b.id for b in batches if b.status=='queued' or not b.started_at or (now-b.started_at).total_seconds()>=STALE_SECONDS]


def create(db,body,user):
    if body.date_from>body.date_to:raise HTTPException(409,'结案日期起止顺序不正确')
    batch=FeedEffectBatch(date_from=body.date_from,date_to=body.date_to,status='queued',created_by=user.email)
    db.add(batch);audit(db,user,f'发起饲喂效果对照配对 结案 {body.date_from} 至 {body.date_to}')
    db.commit();return batch


def retry(db,batch_id,user):
    batch=db.scalar(select(FeedEffectBatch).where(FeedEffectBatch.id==batch_id).with_for_update())
    if not batch:raise HTTPException(404,'对照配对批次不存在')
    if batch.status=='published':raise HTTPException(409,'已发布批次保留当时依据，不能重试覆盖')
    if batch.status=='computing' and batch.started_at and (datetime.utcnow()-batch.started_at).total_seconds()<STALE_SECONDS:
        raise HTTPException(409,'批次仍在计算中，请稍后查看')
    batch.status='queued';batch.error=''
    audit(db,user,f'续跑饲喂效果对照配对批次 {batch.id}')
    db.commit();return batch


def publish(db,batch_id,user):
    """发布批次。若计算后材料有变（补巡检/补处置），发布前重配；发布后冻结当时依据。

    持有批次行锁，与补巡检/补处置并发时用材料指纹双重核对；若材料在重配前后仍在变化，
    放弃本次发布并让用户稍后重试，而不是发布一份与当前材料不符的结果。
    """
    batch=db.scalar(select(FeedEffectBatch).where(FeedEffectBatch.id==batch_id).with_for_update().execution_options(populate_existing=True))
    if not batch:raise HTTPException(404,'对照配对批次不存在')
    if batch.status=='published':return batch,False
    if batch.status not in ['computed','failed']:raise HTTPException(409,'批次尚未完成配对计算，不能发布')
    repaired=False
    for attempt in range(3):
        before=current_fingerprint(db,batch.date_from,batch.date_to)
        if batch.status=='computed' and before==batch.fingerprint:
            break
        _compute(db,batch)  # 补巡检或补处置改变了材料，发布前重配
        batch.revisions=(batch.revisions or 0)+1;repaired=True
        if current_fingerprint(db,batch.date_from,batch.date_to)==batch.fingerprint:break
    else:
        db.rollback()
        raise HTTPException(409,'发布前材料持续变化，请稍后重试')
    batch.published_at=datetime.utcnow()
    batch.snapshot=_snapshot(db,batch)
    batch.status='published'
    audit(db,user,f'发布饲喂效果对照批次 {batch.id}，配成{(batch.summary or {}).get("paired",0)}单'+('，发布前已重配' if repaired else ''))
    db.commit();return batch,repaired


def _row_case(row,hives,visits):
    return {'id':row.id,'care_id':row.care_id,'hive_id':row.hive_id,'hive_code':hives.get(row.hive_id),
            'visit_id':row.visit_id,'visit_code':visits.get(row.visit_id),
            'followup_visit_id':row.followup_visit_id,'followup_visit_code':visits.get(row.followup_visit_id),
            'state':row.state,'reason':row.reason,'baseline_at':_iso(row.baseline_at),'followup_at':_iso(row.followup_at),
            'baseline_weight':row.baseline_weight,'followup_weight':row.followup_weight,
            'baseline_score':row.baseline_score,'followup_score':row.followup_score,'delta':row.delta}


def _row_pair(row,hives,visits,case_map):
    case=case_map.get(row.case_id)
    return {'id':row.id,'case_id':row.case_id,'care_id':case.care_id if case else None,
            'case_hive_code':hives.get(case.hive_id) if case else None,
            'control_hive_id':row.control_hive_id,'control_hive_code':hives.get(row.control_hive_id),
            'control_baseline_visit':visits.get(row.control_baseline_visit_id),
            'control_followup_visit':visits.get(row.control_followup_visit_id),
            'control_baseline_score':row.control_baseline_score,'control_followup_score':row.control_followup_score,
            'control_delta':row.control_delta,'case_delta':row.case_delta,'diff_in_diff':row.diff_in_diff,
            'baseline_gap':row.baseline_gap}


def _tables(db,batch_id):
    cases=db.scalars(select(FeedEffectCase).where(FeedEffectCase.batch_id==batch_id).order_by(FeedEffectCase.id)).all()
    pairs=db.scalars(select(FeedEffectPair).where(FeedEffectPair.batch_id==batch_id).order_by(FeedEffectPair.id)).all()
    hives={h.id:h.code for h in db.scalars(select(Hive)).all()}
    visits={v.id:v.code for v in db.scalars(select(Visit)).all()}
    case_map={c.id:c for c in cases}
    return [_row_case(c,hives,visits) for c in cases],[_row_pair(p,hives,visits,case_map) for p in pairs]


def _snapshot(db,batch):
    cases,pairs=_tables(db,batch.id)
    return {'published_at':_iso(batch.published_at),'fingerprint':batch.fingerprint,'reference_sha':batch.reference_sha,
            'threshold':batch.threshold,'revisions':batch.revisions,'date_from':batch.date_from.isoformat(),
            'date_to':batch.date_to.isoformat(),'summary':batch.summary,'cases':cases,'pairs':pairs}


def detail(db,batch):
    cases,pairs=_tables(db,batch.id)
    paired_ids={p['case_id'] for p in pairs}
    return {'batch':batch,'cases':cases,'pairs':pairs,
            'paired':[c for c in cases if c['id'] in paired_ids],
            'missing':[c for c in cases if c['state']=='missing'],
            'unmatched':[c for c in cases if c['state']=='unmatched'],
            'summary':batch.summary,'snapshot':batch.snapshot}


def frozen_detail(batch):
    """已发布批次从快照读出当时依据，不再关联当前表，避免被后续补录改写。"""
    snap=batch.snapshot or {}
    cases=snap.get('cases',[]);pairs=snap.get('pairs',[])
    paired_ids={p['case_id'] for p in pairs}
    return {'batch':batch,'cases':cases,'pairs':pairs,
            'paired':[c for c in cases if c['id'] in paired_ids],
            'missing':[c for c in cases if c['state']=='missing'],
            'unmatched':[c for c in cases if c['state']=='unmatched'],
            'summary':snap.get('summary'),'snapshot':snap}
