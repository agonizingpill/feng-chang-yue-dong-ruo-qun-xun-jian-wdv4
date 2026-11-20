import os
from datetime import date
from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import select
from pydantic import BaseModel,ConfigDict,field_validator
import redis
from .database import session
from .auth import current,editor,admin
from .feed_effect_models import FeedEffectBatch
from . import feed_effect_service
router=APIRouter()
queue=redis.Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True)


class BatchInput(BaseModel):
    model_config=ConfigDict(allow_inf_nan=False)
    date_from:date
    date_to:date
    @field_validator('date_to')
    @classmethod
    def ordered(cls,v,info):
        if 'date_from' in info.data and v<info.data['date_from']:raise ValueError('结案日期截止不能早于起始')
        return v


def _enqueue(batch_id):
    queue.rpush('apiary:feed-effect',str(batch_id))


@router.get('/feed-effect-batches')
def batches(user=Depends(current),db=Depends(session)):
    return db.scalars(select(FeedEffectBatch).order_by(FeedEffectBatch.id.desc())).all()


@router.post('/feed-effect-batches')
def create(body:BatchInput,user=Depends(editor),db=Depends(session)):
    batch=feed_effect_service.create(db,body,user)
    _enqueue(batch.id)
    return {'id':batch.id,'status':batch.status,'date_from':batch.date_from,'date_to':batch.date_to}


@router.get('/feed-effect-batches/{key}')
def detail(key:int,user=Depends(current),db=Depends(session)):
    batch=db.get(FeedEffectBatch,key)
    if not batch:raise HTTPException(404,'对照配对批次不存在')
    return feed_effect_service.frozen_detail(batch) if batch.status=='published' else feed_effect_service.detail(db,batch)


@router.post('/feed-effect-batches/{key}/retry')
def retry(key:int,user=Depends(editor),db=Depends(session)):
    batch=feed_effect_service.retry(db,key,user)
    _enqueue(batch.id)
    return {'id':batch.id,'status':batch.status}


@router.post('/feed-effect-batches/{key}/publish')
def publish(key:int,user=Depends(admin),db=Depends(session)):
    batch,repaired=feed_effect_service.publish(db,key,user)
    return {'id':batch.id,'status':batch.status,'repaired':repaired,'revisions':batch.revisions,
            'published_at':batch.published_at.isoformat() if batch.published_at else None}
