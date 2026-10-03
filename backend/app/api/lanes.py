import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Lane, RefillOrder
from app.services.fill_engine import build_fill_lines, compute_gap, summarize

router = APIRouter(prefix="/lanes", tags=["lanes"])


def lane_row(r: Lane) -> dict:
    gap = compute_gap(r.capacity, r.stock, r.in_transit)
    return {"id": r.id, "location_id": r.location_id, "slot_no": r.slot_no, "sku_name": r.sku_name,
            "capacity": r.capacity, "stock": r.stock, "in_transit": r.in_transit, "gap": gap,
            "fill_pct": round(r.stock / r.capacity * 100, 1) if r.capacity else 0}


@router.get("")
def list_lanes(location_id: int | None = None, db: Session = Depends(get_db)):
    q = select(Lane).order_by(Lane.slot_no)
    if location_id is not None: q = q.where(Lane.location_id == location_id)
    return [lane_row(r) for r in db.scalars(q).all()]


class LaneCapacityUpdate(BaseModel):
    capacity: int


@router.put("/{lane_id}")
def update_lane_capacity(lane_id: int, body: LaneCapacityUpdate, db: Session = Depends(get_db)):
    """改容量并按新容量重算该点位当前有效补货单的全部行。

    容量、有效单、汇总、满仓同成同败：同一事务提交，任一环节失败整体回滚；
    只改写最新一单，更早的补货单保持原口径。
    """
    lane = db.get(Lane, lane_id)
    if not lane: raise HTTPException(404, "货道不存在")
    if body.capacity <= 0: raise HTTPException(400, "容量必须为正整数")
    try:
        lane.capacity = body.capacity
        order = db.scalars(select(RefillOrder).where(RefillOrder.location_id == lane.location_id)
                           .order_by(RefillOrder.id.desc())).first()
        if order is not None:
            lanes = db.scalars(select(Lane).where(Lane.location_id == lane.location_id)
                               .order_by(Lane.slot_no)).all()
            payload = [{"id": l.id, "slot_no": l.slot_no, "sku_name": l.sku_name,
                        "capacity": l.capacity, "stock": l.stock, "in_transit": l.in_transit}
                       for l in lanes]
            order.lines_json = json.dumps(summarize(build_fill_lines(payload)), ensure_ascii=False)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(500, "容量更新失败，容量与补货单已整体回滚") from exc
    db.refresh(lane)
    return {**lane_row(lane), "recalculated_order_id": order.id if order else None}
