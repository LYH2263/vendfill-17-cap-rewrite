import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import Lane
from app.api.refills import build_location_summary, latest_order
from app.services.fill_engine import compute_gap
router = APIRouter(prefix="/lanes", tags=["lanes"])

@router.get("")
def list_lanes(location_id: int | None = None, db: Session = Depends(get_db)):
    q = select(Lane).order_by(Lane.slot_no)
    if location_id is not None: q = q.where(Lane.location_id == location_id)
    out = []
    for r in db.scalars(q).all():
        gap = compute_gap(r.capacity, r.stock, r.in_transit)
        out.append({"id": r.id, "location_id": r.location_id, "slot_no": r.slot_no, "sku_name": r.sku_name,
                    "capacity": r.capacity, "stock": r.stock, "in_transit": r.in_transit, "gap": gap,
                    "fill_pct": round(r.stock / r.capacity * 100, 1) if r.capacity else 0})
    return out


class CapacityUpdate(BaseModel):
    capacity: int


@router.patch("/{lane_id}")
def update_capacity(lane_id: int, body: CapacityUpdate, db: Session = Depends(get_db)):
    # 容量必须为正：直接拒绝，四处（容量/有效单/汇总/满仓）不动。
    if isinstance(body.capacity, bool) or body.capacity <= 0:
        raise HTTPException(400, "容量必须为正整数")
    lane = db.get(Lane, lane_id)
    if not lane:
        raise HTTPException(404, "货道不存在")

    # 容量、有效单重算、汇总、满仓同成同败：一个事务，任一异常全部回滚。
    try:
        location_id = lane.location_id
        lane.capacity = body.capacity
        db.flush()  # 让容量改动落进事务；提交失败会随单据一起撤销

        order = latest_order(db, location_id)
        if order is not None:
            # 按新容量重算该单全部行并写回同一张有效单（id 不变）；更早的单据不触碰。
            summary = build_location_summary(db, location_id)
            order.lines_json = json.dumps(summary, ensure_ascii=False)

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:  # 重算/写回失败：撤销容量改动与全部单据写回
        db.rollback()
        raise HTTPException(500, f"容量更新已回滚：{exc}")

    db.refresh(lane)
    gap = compute_gap(lane.capacity, lane.stock, lane.in_transit)
    return {"id": lane.id, "location_id": lane.location_id, "slot_no": lane.slot_no,
            "sku_name": lane.sku_name, "capacity": lane.capacity, "stock": lane.stock,
            "in_transit": lane.in_transit, "gap": gap,
            "fill_pct": round(lane.stock / lane.capacity * 100, 1) if lane.capacity else 0,
            "refill_order_id": order.id if order is not None else None}
