import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.main import create_app
from app.service import Service


@pytest.fixture()
def db(tmp_path):
    return Database(str(tmp_path / "ledger.db"))


@pytest.fixture()
def service(db):
    return Service(db, "2026-11-25")


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "api.db"))
    with TestClient(app) as c:
        c.db = app.state.db
        c.svc = app.state.service
        yield c


def quote(client, no="Q1", s="USD", t="CNY", lo="7", hi="7.2"):
    return client.post("/quotes", json={
        "quote_no": no, "source": s, "target": t, "lower": lo, "upper": hi})


def order(no="O1", s="USD", t="CNY", a="100", b="710"):
    return {"order_no": no, "source": s, "target": t,
            "original_amount": a, "declared_amount": b}


def batch(client, orders):
    return client.post("/batches", json={"orders": orders})
