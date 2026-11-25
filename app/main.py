"""FastAPI entrypoint for the single-ledger daily-rate write-off API.

Run locally:  ``uvicorn app.main:app --reload``
Then open:    http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import os
from datetime import date

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from .db import Database
from .schemas import BatchCreate, QuoteCreate, QuoteUpdate
from .service import Rejection, Service

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "data/ledger.db")
PRICING_DATE = os.environ.get("PRICING_DATE", date.today().isoformat())


def create_app(db_path: str | None = None) -> FastAPI:
    db = Database(db_path or DEFAULT_DB_PATH)
    service = Service(db, PRICING_DATE)

    app = FastAPI(
        title="同一账簿 · 日汇率外币报销核销 API",
        description=(
            "一个计价日的 SQLite 核销账本。\n\n"
            "- 在 **/docs** 按报价号「登记 / 改区间 / 撤回」正分数闭区间报价；\n"
            "- 报销单 **按批提交**，整批判定、原子核销；\n"
            "- 最多 32 种币，全部金额/汇率使用精确十进制与分数运算；\n"
            "- 有效报价 + 已核销单连成币组，核销只读所涉币组汇总。\n\n"
            "所有金额与区间端点请填 **字符串**（如 `\"7.15\"` 或 `\"143/20\"`），"
            "以保证按原值精确核对。"
        ),
        version="1.0.0",
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.db = db
    app.state.service = service

    @app.exception_handler(Rejection)
    async def _rejection_handler(_: Request, exc: Rejection):
        return JSONResponse(
            status_code=exc.status_code,
            content={"accepted": False, "error": exc.error, "reasons": exc.reasons},
        )

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse(url="/docs")

    @app.get("/health", tags=["系统"], summary="健康检查 / 账本概况")
    def health():
        return {"status": "ok", **service.meta()}

    @app.get("/meta", tags=["系统"], summary="计价日与计数")
    def meta():
        return service.meta()

    # ------------------------------ quotes ------------------------------ #

    @app.post("/quotes", tags=["报价"],
              summary="按报价号登记报价（正分数闭区间）")
    def create_quote(body: QuoteCreate):
        """登记一条 ``1 source = [lower, upper] target`` 的闭区间报价。

        即使该报价与旧核销单冲突也会入档（仍返回 200，并在 ``groups`` 中把
        对应币组标为 ``suspended``）；随后用 PUT 改正区间即可自动恢复。
        """
        return service.create_quote(body)

    @app.put("/quotes/{quote_no}", tags=["报价"],
             summary="按报价号改区间（并使报价有效，可用于改正后恢复）")
    def update_quote(quote_no: str, body: QuoteUpdate):
        return service.update_quote(quote_no, body)

    @app.post("/quotes/{quote_no}/withdraw", tags=["报价"],
              summary="撤回报价（旧报价留档，币组可能分裂）")
    def withdraw_quote(quote_no: str):
        return service.withdraw_quote(quote_no)

    @app.delete("/quotes/{quote_no}", tags=["报价"],
                summary="撤回报价（DELETE 别名，幂等）")
    def delete_quote(quote_no: str):
        return service.withdraw_quote(quote_no)

    @app.get("/quotes", tags=["报价"], summary="列出报价（可按状态过滤）")
    def list_quotes(status: str | None = Query(None, pattern="^(active|withdrawn)$")):
        return {"quotes": service.list_quotes(status)}

    @app.get("/quotes/{quote_no}", tags=["报价"], summary="按报价号查报价")
    def get_quote(quote_no: str):
        q = service.get_quote(quote_no)
        if q is None:
            raise Rejection(404, "NOT_FOUND",
                            [{"code": "QUOTE_NOT_FOUND",
                              "message": f"报价号 {quote_no} 不存在"}])
        return q

    # ------------------------------ batches ----------------------------- #

    @app.post("/batches", tags=["核销"],
              summary="按批提交报销单（缺链/重号/矛盾则整批照旧）")
    def submit_batch(body: BatchCreate):
        return service.submit_batch(body)

    @app.get("/orders/{order_no}", tags=["核销"],
             summary="按单号回查核销记录与当时各组精确折算表")
    def get_order(order_no: str):
        o = service.get_order(order_no)
        if o is None:
            raise Rejection(404, "NOT_FOUND",
                            [{"code": "ORDER_NOT_FOUND",
                              "message": f"单号 {order_no} 尚无核销记录"}])
        return o

    @app.get("/batches/{batch_id}", tags=["核销"],
             summary="按批次号查批次与留存折算表")
    def get_batch(batch_id: str):
        b = service.get_batch(batch_id)
        if b is None:
            raise Rejection(404, "NOT_FOUND",
                            [{"code": "BATCH_NOT_FOUND",
                              "message": f"批次 {batch_id} 不存在"}])
        return b

    # ------------------------------ groups ------------------------------ #

    @app.get("/groups", tags=["币组"], summary="列出当前币组与状态/汇总版本")
    def list_groups():
        return {"groups": service.list_groups()}

    @app.get("/groups/{group_id}", tags=["币组"],
             summary="查币组汇总（精确根币势与闭包矩阵）")
    def get_group(group_id: str):
        g = service.get_group(group_id)
        if g is None:
            raise Rejection(404, "NOT_FOUND",
                            [{"code": "GROUP_NOT_FOUND",
                              "message": f"币组 {group_id} 不存在（报价变动后币组号会更新）"}])
        return g

    return app


app = create_app()
