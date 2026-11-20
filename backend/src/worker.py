import os,json,traceback,time
from pathlib import Path
import redis
from sqlalchemy import select
from .database import Session
from .models import Visit,Hive
from .screen_model import screen,reference_model
from .yard_service import latest
from . import feed_effect_service
queue=redis.Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True)
RESUME_EVERY=15
last_resume=0.0


def run_feed_effect(batch_id):
    with Session() as db:
        result=feed_effect_service.compute_batch(db,int(batch_id))
    Path('/artifacts').mkdir(exist_ok=True)
    Path(f'/artifacts/feed-effect-{int(batch_id)}.json').write_text(json.dumps(result,ensure_ascii=False,default=str))
    return result


def resume_pending():
    with Session() as db:
        ids=feed_effect_service.resume_ids(db)
    for batch_id in ids:
        try:run_feed_effect(batch_id)
        except Exception:traceback.print_exc();time.sleep(1)


while True:
    Path('/tmp/apiary-heartbeat').touch()
    if time.monotonic()-last_resume>=RESUME_EVERY:
        last_resume=time.monotonic()
        try:resume_pending()
        except Exception:traceback.print_exc()
    job=queue.lpop('apiary:feed-effect')
    if job:
        try:run_feed_effect(job)
        except Exception:traceback.print_exc()
        continue
    job=queue.blpop('apiary:screening',timeout=3)
    if not job:continue
    task=json.loads(job[1]);points=[];evidence=reference_model()[1]
    try:
        for key in task['visit_ids']:
            Path('/tmp/apiary-heartbeat').touch()
            with Session() as db:
                visit=db.scalar(select(Visit).where(Visit.id==key).with_for_update())
                if not visit or visit.status!='screening':continue
                score,flag,margin,evidence=screen(visit)
                visit.anomaly_score=score;visit.anomaly_flag=flag;visit.decision_margin=margin;visit.model_evidence=evidence;visit.status='screened'
                hive=db.get(Hive,visit.hive_id)
                if latest(db,hive.id)==visit.id:hive.care_state='needs_care' if flag or not visit.queen_seen else 'observe'
                db.commit();points.append({'visit_id':visit.id,'hive_id':hive.id,'score':score,'flag':flag})
        result={'status':'done','count':len(points),'points':points,'evidence':evidence}
        Path('/artifacts').mkdir(exist_ok=True);Path(f"/artifacts/screen-{task['job_id']}.json").write_text(json.dumps(result))
        queue.set('apiary:screen:'+task['job_id'],json.dumps(result),ex=86400)
    except Exception as error:
        traceback.print_exc();queue.set('apiary:screen:'+task['job_id'],json.dumps({'status':'failed','error':str(error)}),ex=86400)
        with Session() as db:
            for v in db.scalars(select(Visit).where(Visit.id.in_(task['visit_ids']),Visit.status=='screening')).all():v.status='blocked'
            db.commit()
