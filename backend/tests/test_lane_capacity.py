"""改容量强制重算当前有效补货单：容量、单据、汇总、满仓同成同败。"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import lanes as lanes_api
from app.database import Base, get_db
from app.main import app
from app.models.models import RefillOrder
from app.services.seed import seed_if_empty

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    seed_if_empty(db)  # A1(20,5,0) A2(18,18,0) B1(12,3,2) B2(15,10,5) C1(10,0,0) C2(24,24,2)
    db.close()
    return TestClient(app)  # 不进上下文管理器，避免 lifespan 连真实库


def lane_id_of(client, slot_no):
    lanes = client.get("/api/lanes?location_id=1").json()
    return next(l for l in lanes if l["slot_no"] == slot_no)["id"]


def test_shrink_recalculates_active_order_and_summary_only(client):
    older = client.post("/api/refills/run?location_id=1").json()
    active = client.post("/api/refills/run?location_id=1").json()
    assert older["id"] != active["id"]
    a1 = next(l for l in active["lines"] if l["slot_no"] == "A1")
    assert (a1["capacity"], a1["fill_qty"]) == (20, 15)
    assert active["total_fill"] == 32

    r = client.put(f"/api/lanes/{lane_id_of(client, 'A1')}", json={"capacity": 8})
    assert r.status_code == 200
    assert r.json()["capacity"] == 8
    assert r.json()["recalculated_order_id"] == active["id"]

    # 有效单被就地重算：同一单号，A1 补量变小
    latest = client.get("/api/refills/latest?location_id=1").json()
    assert latest["id"] == active["id"]
    a1_after = next(l for l in latest["lines"] if l["slot_no"] == "A1")
    assert (a1_after["capacity"], a1_after["gap"], a1_after["fill_qty"]) == (8, 3, 3)
    # 汇总同步变成新口径
    assert latest["total_fill"] == 20
    summary = client.get("/api/refills/summary?location_id=1").json()
    assert summary["total_fill"] == 20
    # 货道容量同步
    lanes = client.get("/api/lanes?location_id=1").json()
    assert next(l for l in lanes if l["slot_no"] == "A1")["capacity"] == 8
    # 更早的补货单保持原口径
    db = TestingSessionLocal()
    older_row = db.get(RefillOrder, older["id"])
    db.close()
    older_data = json.loads(older_row.lines_json)
    assert older_data["total_fill"] == 32
    assert next(l for l in older_data["lines"] if l["slot_no"] == "A1")["fill_qty"] == 15

    # 再提交非法容量：全部回滚，四处仍是改后（新）口径
    r = client.put(f"/api/lanes/{lane_id_of(client, 'A1')}", json={"capacity": 0})
    assert r.status_code == 400
    lanes = client.get("/api/lanes?location_id=1").json()
    assert next(l for l in lanes if l["slot_no"] == "A1")["capacity"] == 8
    latest = client.get("/api/refills/latest?location_id=1").json()
    assert latest["id"] == active["id"] and latest["total_fill"] == 20
    assert client.get("/api/refills/summary?location_id=1").json()["total_fill"] == 20


def test_nonpositive_capacity_rejected_nothing_changes(client):
    active = client.post("/api/refills/run?location_id=1").json()
    a1_id = lane_id_of(client, "A1")
    for bad in (0, -3):
        r = client.put(f"/api/lanes/{a1_id}", json={"capacity": bad})
        assert r.status_code == 400
    r = client.put("/api/lanes/99999", json={"capacity": 5})
    assert r.status_code == 404
    lanes = client.get("/api/lanes?location_id=1").json()
    assert next(l for l in lanes if l["slot_no"] == "A1")["capacity"] == 20
    latest = client.get("/api/refills/latest?location_id=1").json()
    assert latest["id"] == active["id"] and latest["total_fill"] == 32
    assert client.get("/api/refills/summary?location_id=1").json()["total_fill"] == 32


def test_recalc_failure_rolls_back_capacity_and_order(client, monkeypatch):
    active = client.post("/api/refills/run?location_id=1").json()

    def boom(_lanes, requested=None):
        raise RuntimeError("recalc exploded")

    monkeypatch.setattr(lanes_api, "build_fill_lines", boom)
    r = client.put(f"/api/lanes/{lane_id_of(client, 'A1')}", json={"capacity": 8})
    monkeypatch.undo()
    assert r.status_code == 500
    # 容量与有效单一起回滚，四处仍显示改前
    lanes = client.get("/api/lanes?location_id=1").json()
    assert next(l for l in lanes if l["slot_no"] == "A1")["capacity"] == 20
    latest = client.get("/api/refills/latest?location_id=1").json()
    assert latest["id"] == active["id"] and latest["total_fill"] == 32
    a1 = next(l for l in latest["lines"] if l["slot_no"] == "A1")
    assert (a1["capacity"], a1["fill_qty"]) == (20, 15)


def test_full_list_tracks_capacity_changes(client):
    client.post("/api/refills/run?location_id=1").json()
    full0 = client.get("/api/refills/full?location_id=1").json()["lanes"]
    assert {l["slot_no"] for l in full0} == {"A2", "B2"}

    # A1 容量降到库存：进入满仓，满仓页与单行状态一致
    r = client.put(f"/api/lanes/{lane_id_of(client, 'A1')}", json={"capacity": 5})
    assert r.status_code == 200
    latest = client.get("/api/refills/latest?location_id=1").json()
    a1 = next(l for l in latest["lines"] if l["slot_no"] == "A1")
    assert (a1["status"], a1["fill_qty"]) == ("full", 0)
    full1 = client.get("/api/refills/full?location_id=1").json()["lanes"]
    assert {l["slot_no"] for l in full1} == {"A1", "A2", "B2"}
    assert client.get("/api/refills/summary?location_id=1").json()["full_count"] == 3

    # A2 容量调大：离开满仓，满仓页同步移除
    r = client.put(f"/api/lanes/{lane_id_of(client, 'A2')}", json={"capacity": 20})
    assert r.status_code == 200
    latest = client.get("/api/refills/latest?location_id=1").json()
    a2 = next(l for l in latest["lines"] if l["slot_no"] == "A2")
    assert (a2["status"], a2["fill_qty"]) == ("need_fill", 2)
    full2 = client.get("/api/refills/full?location_id=1").json()["lanes"]
    assert {l["slot_no"] for l in full2} == {"A1", "B2"}
    assert client.get("/api/refills/summary?location_id=1").json()["full_count"] == 2
