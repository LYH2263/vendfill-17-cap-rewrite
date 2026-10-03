"""改容量触发有效补货单强制重算的集成测试。

四处同成同败：货道容量、有效单全部行、汇总、满仓名单；
容量 <= 0 直接拒绝；任一环节异常整体回滚；更早单据不被改写。
"""
import json
import os
import tempfile

import pytest

# 必须在 import app.* 之前把数据库指到临时 sqlite（pydantic-settings 读环境变量）
_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_db_file.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file.name}"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.models import Lane, RefillOrder  # noqa: E402
from app.services.seed import seed_if_empty  # noqa: E402

LOCATION_ID = 1


@pytest.fixture
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_if_empty(db)  # A1 水 cap20 stock5；A2 可乐 cap18 stock18（满仓）
    finally:
        db.close()
    return TestClient(app)


def _line(data, slot):
    return next(l for l in data["lines"] if l["slot_no"] == slot)


def test_recompute_active_order_and_summary_after_capacity_shrink(client):
    # 种子下先生成一张有效补货单：A1 缺口 15，总补量 32
    run = client.post("/api/refills/run", params={"location_id": LOCATION_ID}).json()
    assert run["id"] == 1
    assert _line(run, "A1")["fill_qty"] == 15
    assert run["total_fill"] == 32

    # 改小 A1 容量 20 -> 10
    resp = client.patch("/api/lanes/1", json={"capacity": 10})
    assert resp.status_code == 200, resp.text
    assert resp.json()["capacity"] == 10

    # 当前有效单仍是同一张（id 不变），A1 行按新口径重算，补量 15 -> 5
    latest = client.get("/api/refills/latest", params={"location_id": LOCATION_ID}).json()
    assert latest["id"] == 1
    a1 = _line(latest, "A1")
    assert a1["capacity"] == 10
    assert a1["gap"] == 5
    assert a1["fill_qty"] == 5
    assert latest["total_fill"] == 22

    # 汇总同口径
    summary = client.get("/api/refills/summary", params={"location_id": LOCATION_ID}).json()
    assert summary["total_fill"] == 22
    assert summary["full_count"] == latest["full_count"]

    # 货道页容量已是新值
    lanes = {r["slot_no"]: r for r in client.get("/api/lanes").json()}
    assert lanes["A1"]["capacity"] == 10
    assert lanes["A1"]["gap"] == 5


def test_full_page_matches_line_status_when_leaving_and_entering_full(client):
    client.post("/api/refills/run", params={"location_id": LOCATION_ID})

    def full_slots():
        full = client.get("/api/refills/full", params={"location_id": LOCATION_ID}).json()["lanes"]
        return {l["slot_no"] for l in full}

    # A2（18/18）初始满仓，且满仓页与单行 status 一致
    latest = client.get("/api/refills/latest", params={"location_id": LOCATION_ID}).json()
    assert _line(latest, "A2")["status"] == "full"
    assert "A2" in full_slots()

    # 放大容量 18 -> 20：A2 离开满仓，满仓页必须同步移除
    assert client.patch("/api/lanes/2", json={"capacity": 20}).status_code == 200
    latest = client.get("/api/refills/latest", params={"location_id": LOCATION_ID}).json()
    assert _line(latest, "A2")["status"] == "need_fill"
    assert "A2" not in full_slots()

    # 改回 18：A2 重新进入满仓，满仓页必须立即补回
    assert client.patch("/api/lanes/2", json={"capacity": 18}).status_code == 200
    latest = client.get("/api/refills/latest", params={"location_id": LOCATION_ID}).json()
    assert _line(latest, "A2")["status"] == "full"
    assert "A2" in full_slots()

    # 强一致：满仓页名单 == 有效单中 status=='full' 的行集合
    expect = {l["slot_no"] for l in latest["lines"] if l["status"] == "full"}
    assert full_slots() == expect


def test_earlier_orders_are_not_rewritten(client):
    # 第一张更早的单（旧口径：A1 fill 15，总量 32）
    first = client.post("/api/refills/run", params={"location_id": LOCATION_ID}).json()
    assert first["id"] == 1
    # 再来一张成为当前有效单
    second = client.post("/api/refills/run", params={"location_id": LOCATION_ID}).json()
    assert second["id"] == 2

    assert client.patch("/api/lanes/1", json={"capacity": 10}).status_code == 200

    # 当前有效单被重算……
    latest = client.get("/api/refills/latest", params={"location_id": LOCATION_ID}).json()
    assert latest["id"] == 2
    assert _line(latest, "A1")["fill_qty"] == 5

    # ……更早的第一张单保持旧口径，不被改写
    db = SessionLocal()
    try:
        old = db.get(RefillOrder, 1)
        old_data = json.loads(old.lines_json)
        assert old_data["total_fill"] == 32
        assert next(l for l in old_data["lines"] if l["slot_no"] == "A1")["fill_qty"] == 15
    finally:
        db.close()


def test_non_positive_capacity_rejected_all_four_views_unchanged(client):
    run = client.post("/api/refills/run", params={"location_id": LOCATION_ID}).json()
    assert _line(run, "A1")["fill_qty"] == 15

    for bad in (0, -5):
        resp = client.patch("/api/lanes/1", json={"capacity": bad})
        assert resp.status_code == 400, bad

    # 容量未变
    db = SessionLocal()
    try:
        assert db.get(Lane, 1).capacity == 20
    finally:
        db.close()

    # 有效单 / 汇总 / 满仓 仍是改前口径
    latest = client.get("/api/refills/latest", params={"location_id": LOCATION_ID}).json()
    assert latest["id"] == 1
    assert _line(latest, "A1")["capacity"] == 20
    assert _line(latest, "A1")["fill_qty"] == 15
    assert latest["total_fill"] == 32
    summary = client.get("/api/refills/summary", params={"location_id": LOCATION_ID}).json()
    assert summary["total_fill"] == 32
    full = {l["slot_no"] for l in client.get("/api/refills/full",
                                             params={"location_id": LOCATION_ID}).json()["lanes"]}
    expect = {l["slot_no"] for l in latest["lines"] if l["status"] == "full"}
    assert full == expect


def test_illegal_after_legal_change_rolls_back_keeping_new_baseline(client):
    # 先合法改小成功（四处已是新口径）
    assert client.patch("/api/lanes/1", json={"capacity": 10}).status_code == 200
    # 再提交非法容量：本次事务全部回滚，四处保持在“合法改小后”的口径
    assert client.patch("/api/lanes/1", json={"capacity": 0}).status_code == 400

    db = SessionLocal()
    try:
        assert db.get(Lane, 1).capacity == 10
    finally:
        db.close()
    latest = client.get("/api/refills/latest", params={"location_id": LOCATION_ID}).json()
    assert _line(latest, "A1")["capacity"] == 10
    assert _line(latest, "A1")["fill_qty"] == 5
    assert latest["total_fill"] == 22


def test_recompute_failure_rolls_back_capacity_and_order(client, monkeypatch):
    client.post("/api/refills/run", params={"location_id": LOCATION_ID})

    def boom(*_a, **_k):
        raise RuntimeError("recompute exploded")

    # 重算环节炸掉：模拟“任一环节失败”
    monkeypatch.setattr("app.api.lanes.build_location_summary", boom)

    resp = client.patch("/api/lanes/1", json={"capacity": 10})
    assert resp.status_code == 500

    # 容量改动撤销
    db = SessionLocal()
    try:
        assert db.get(Lane, 1).capacity == 20
        order = db.query(RefillOrder).order_by(RefillOrder.id.desc()).first()
        data = json.loads(order.lines_json)
        assert next(l for l in data["lines"] if l["slot_no"] == "A1")["fill_qty"] == 15
        assert data["total_fill"] == 32
    finally:
        db.close()

    # 四处仍显示改前
    latest = client.get("/api/refills/latest", params={"location_id": LOCATION_ID}).json()
    assert _line(latest, "A1")["capacity"] == 20
    assert latest["total_fill"] == 32
    assert client.get("/api/refills/summary", params={"location_id": LOCATION_ID}).json()["total_fill"] == 32
