"""订单级坏行暂缓、补正重算与不重复签发的流程测试。

对应需求：
- 可归属坏行只暂缓相关订单，其他订单照常签发；
- 同一商品行挂两张订单，两张都暂缓；
- 不可归属坏行整批暂缓；
- 补正后旧待签金额失效，按完整订单重算（优惠/运费）；
- 已签发金额与凭证原样保留；
- 补正失败可重试，同一订单不重复签发。
"""
import pytest


def row(**changes):
    item = dict(order_id="O1", line_id="O1L1", line_amount="10.00", discount_total="0.00",
                return_qty="1", qty="1", unit_refund="10.00", freight="0.00", payment="100.00")
    item.update(changes)
    return item


def create_with_rows(client, rows):
    response = client.post("/api/runs", json={"rows": rows})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def issue(client, run_id, order_id, **body):
    body.setdefault("expected_revision", None)
    return client.post(f"/api/runs/{run_id}/orders/{order_id}/issue", json=body)


def correct(client, run_id, order_id, **body):
    return client.post(f"/api/runs/{run_id}/orders/{order_id}/correct", json=body)


# ---------------------------------------------------------------------------
# 归属与暂缓范围
# ---------------------------------------------------------------------------
def test_attributable_bad_row_holds_only_its_order(client):
    run = create_with_rows(client, [
        row(order_id="O1", line_id="O1L1", line_amount="not-money"),
        row(order_id="O2", line_id="O2L1", line_amount="20.00", unit_refund="20.00", payment="20.00"),
    ])
    states = {o["order_id"]: o["status"] for o in run["review_board"]["orders"]}
    assert states["O1"] == "HELD"
    assert states["O2"] == "READY"
    assert run["review_board"]["batch_hold"] is False

    blocked = issue(client, run["run_id"], "O1")
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] in {"ORDER_HELD", "GATE_BLOCKED"}

    ok = issue(client, run["run_id"], "O2")
    assert ok.status_code == 200
    assert ok.get_json()["status"] == "ISSUED"


def test_same_product_line_on_two_orders_holds_both(client):
    run = create_with_rows(client, [
        row(order_id="O3", line_id="DUP", line_amount="10.00"),
        row(order_id="O4", line_id="DUP", line_amount="12.00"),
        row(order_id="O5", line_id="O5L1", line_amount="5.00", unit_refund="5.00", payment="5.00"),
    ])
    board = run["review_board"]
    by_id = board["order_map"]
    assert by_id["O3"]["status"] == "HELD"
    assert by_id["O4"]["status"] == "HELD"
    dup_attrs = [a for a in board["attributions"] if "O3" in a["order_ids"] and "O4" in a["order_ids"]]
    assert dup_attrs
    # 第三张订单不受牵连
    assert by_id["O5"]["status"] == "READY"
    assert issue(client, run["run_id"], "O5").status_code == 200
    for oid in ("O3", "O4"):
        response = issue(client, run["run_id"], oid)
        assert response.status_code == 409


def test_unattributable_bad_row_holds_whole_batch(client):
    run = create_with_rows(client, [
        row(order_id="O6", line_id="O6L1", line_amount="7.00", unit_refund="7.00", payment="7.00"),
        {"order_id": "", "line_id": "", "line_amount": "???", "discount_total": "",
         "return_qty": "", "qty": "", "unit_refund": "", "freight": "", "payment": ""},
    ])
    assert run["review_board"]["batch_hold"] is True
    assert run["review_board"]["batch_status"] == "HELD"
    response = issue(client, run["run_id"], "O6")
    assert response.status_code == 409
    assert response.get_json()["code"] == "BATCH_HELD"


def test_blank_row_is_not_an_unattributable_bad_row(client):
    # 空行属于被忽略记录，不应触发整批暂缓。
    run = create_with_rows(client, [
        row(order_id="O1", line_id="O1L1"),
        {c: "" for c in ["order_id", "line_id", "line_amount", "discount_total",
                         "return_qty", "qty", "unit_refund", "freight", "payment"]},
    ])
    assert run["review_board"]["batch_hold"] is False
    assert run["order_reviews"]["O1"]["status"] == "READY"


# ---------------------------------------------------------------------------
# 补正与重算
# ---------------------------------------------------------------------------
def test_correction_recomputes_full_order_discount(client):
    # 完整 O1（与 tests/domain_cases.CLEAN 前两行一致）应退 4500 分；
    # 第二行坏掉时只按单行暂算为 4250。
    run = create_with_rows(client, [
        row(order_id="O1", line_id="O1L1", line_amount="100.00", discount_total="15.00",
            return_qty="1", qty="2", freight="10.00", payment="200.00"),
        row(order_id="O1", line_id="O1L2", line_amount="bad", discount_total="15.00",
            return_qty="0", qty="1", freight="10.00", payment="200.00"),
    ])
    rid = run["run_id"]
    before = run["order_reviews"]["O1"]
    assert before["status"] == "HELD"
    assert before["amount"] == 4250

    response = correct(client, rid, "O1", values={
        "line_id": "O1L2", "line_amount": "50.00", "return_qty": "0", "qty": "1"})
    assert response.status_code == 200, response.get_data(as_text=True)
    order = response.get_json()["order"]
    assert order["status"] == "READY"
    assert order["amount"] == 4500
    assert order["invalidated_amount"] == 4250
    assert order["amount_version"] >= 2

    # 旧金额版本不能再签发
    stale = issue(client, rid, "O1", expected_amount_version=1)
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "STALE_AMOUNT"

    ok = issue(client, rid, "O1", expected_amount_version=order["amount_version"])
    assert ok.status_code == 200
    assert ok.get_json()["voucher"]["amount"] == 4500


def test_correction_restores_freight_rule(client):
    # 第二行（不退货）缺失时，单行被误判为整单退货而退运费 1000；
    # 补回后不是所有行都退，运费为 0。
    run = create_with_rows(client, [
        row(order_id="OF", line_id="F1", line_amount="100.00", return_qty="1", qty="1",
            freight="10.00", payment="200.00"),
        row(order_id="OF", line_id="F2", line_amount="bad", return_qty="0", qty="1",
            freight="10.00", payment="200.00"),
    ])
    rid = run["run_id"]
    assert run["order_reviews"]["OF"]["amount"] == 11000
    response = correct(client, rid, "OF", values={
        "line_id": "F2", "line_amount": "50.00", "return_qty": "0", "qty": "1"})
    order = response.get_json()["order"]
    assert order["amount"] == 10000
    assert order["invalidated_amount"] == 11000


def test_failed_correction_can_be_retried(client):
    run = create_with_rows(client, [
        row(order_id="O7", line_id="O7L1", return_qty="3", qty="2", payment="20.00"),
    ])
    rid = run["run_id"]
    bad = correct(client, rid, "O7", values={"return_qty": "9", "qty": "2"})
    assert bad.status_code == 422
    assert bad.get_json()["code"] == "CORRECTION_FAILED"

    good = correct(client, rid, "O7", values={"return_qty": "1", "qty": "1"})
    assert good.status_code == 200
    assert good.get_json()["order"]["status"] == "READY"


# ---------------------------------------------------------------------------
# 已签发保留 + 幂等
# ---------------------------------------------------------------------------
def test_issued_amount_and_voucher_are_preserved(client):
    run = create_with_rows(client, [
        row(order_id="O7", line_id="O7L1", line_amount="20.00", payment="20.00"),
        row(order_id="O8", line_id="O8L1", line_amount="11.00", payment="11.00"),
    ])
    rid = run["run_id"]
    first = issue(client, rid, "O8", idempotency_key="k-o8")
    assert first.status_code == 200
    voucher_before = client.get(f"/api/runs/{rid}/vouchers/O8").get_json()

    # 之后对 O7 的任何补正/重算都不得改动 O8 凭证
    correct(client, rid, "O7", values={"freight": "0.00"})
    voucher_after = client.get(f"/api/runs/{rid}/vouchers/O8").get_json()
    assert voucher_after["voucher_sha256"] == voucher_before["voucher_sha256"]
    assert voucher_after["amount"] == voucher_before["amount"]

    fetched = client.get(f"/api/runs/{rid}").get_json()
    assert fetched["order_reviews"]["O8"]["status"] == "ISSUED"
    assert fetched["order_reviews"]["O8"]["issued_amount"] == 1100


def test_correcting_an_issued_order_keeps_its_voucher(client):
    run = create_with_rows(client, [
        row(order_id="O88", line_id="L1", line_amount="10.00", payment="100.00"),
        row(order_id="O88", line_id="L2", line_amount="5.00", return_qty="0", payment="100.00"),
    ])
    rid = run["run_id"]
    assert issue(client, rid, "O88").status_code == 200
    before = client.get(f"/api/runs/{rid}/vouchers/O88").get_json()

    response = correct(client, rid, "O88", line_id="L2", values={"line_amount": "99.00"})
    assert response.status_code == 200
    after = client.get(f"/api/runs/{rid}/vouchers/O88").get_json()
    assert after["voucher_sha256"] == before["voucher_sha256"]
    fetched = client.get(f"/api/runs/{rid}").get_json()
    order = fetched["order_reviews"]["O88"]
    assert order["status"] == "ISSUED"
    assert order["issued_amount"] == before["amount"]
    assert len(client.get(f"/api/runs/{rid}/vouchers").get_json()["vouchers"]) == 1


def test_same_order_is_never_issued_twice(client):
    run = create_with_rows(client, [row(order_id="O9", line_id="O9L1", payment="10.00")])
    rid = run["run_id"]
    first = issue(client, rid, "O9")
    voucher_id = first.get_json()["voucher"]["voucher_id"]

    second = issue(client, rid, "O9")
    assert second.status_code == 200
    body = second.get_json()
    assert body["idempotent"] is True
    assert body["voucher"]["voucher_id"] == voucher_id

    vouchers = client.get(f"/api/runs/{rid}/vouchers").get_json()["vouchers"]
    assert len([v for v in vouchers if v["order_id"] == "O9"]) == 1


def test_retry_with_same_idempotency_key_returns_one_voucher(client):
    run = create_with_rows(client, [row(order_id="O10", line_id="O10L1", payment="10.00")])
    rid = run["run_id"]
    url = f"/api/runs/{rid}/retry"
    payload = {"order_id": "O10", "idempotency_key": "retry-abc"}
    one = client.post(url, json=payload)
    two = client.post(url, json=payload)
    assert one.status_code == 200 and two.status_code == 200
    assert one.get_json()["voucher"]["voucher_id"] == two.get_json()["voucher"]["voucher_id"]
    vouchers = client.get(f"/api/runs/{rid}/vouchers").get_json()["vouchers"]
    assert len(vouchers) == 1


def test_review_endpoint_accepts_order_scoped_decision(client):
    run = create_with_rows(client, [row(order_id="O11", line_id="O11L1", payment="10.00")])
    rid = run["run_id"]
    response = client.post(f"/api/runs/{rid}/review",
                           json={"decision": "APPROVED", "order_id": "O11", "expected_revision": 0})
    assert response.status_code == 200
    assert response.get_json()["status"] == "ISSUED"
    # 旧的整批复核版本号语义仍在
    run_again = client.get(f"/api/runs/{rid}").get_json()
    assert run_again["revision"] >= 1


def test_failed_then_successful_retry_issues_once(client):
    run = create_with_rows(client, [
        row(order_id="O12", line_id="O12L1", return_qty="3", qty="2", payment="20.00")])
    rid = run["run_id"]
    url = f"/api/runs/{rid}/retry"
    key = "combined-retry-key"
    failed = client.post(url, json={"order_id": "O12", "idempotency_key": key,
                                    "and_issue": True, "values": {"return_qty": "9", "qty": "2"}})
    assert failed.status_code == 422
    assert client.get(f"/api/runs/{rid}/vouchers").get_json()["vouchers"] == []

    ok = client.post(url, json={"order_id": "O12", "idempotency_key": key,
                                "and_issue": True, "values": {"return_qty": "1", "qty": "1"}})
    assert ok.status_code == 200
    again = client.post(url, json={"order_id": "O12", "idempotency_key": key,
                                   "and_issue": True, "values": {"return_qty": "1", "qty": "1"}})
    assert again.status_code == 200
    assert again.get_json().get("idempotent") is True
    vouchers = client.get(f"/api/runs/{rid}/vouchers").get_json()["vouchers"]
    assert len(vouchers) == 1


def test_corrections_accept_line_keyed_map(client):
    run = create_with_rows(client, [
        row(order_id="OM", line_id="OML1", return_qty="4", qty="2")])
    rid = run["run_id"]
    response = client.post(f"/api/runs/{rid}/corrections",
                           json={"corrections": {"OML1": {"return_qty": "1"}}})
    assert response.status_code == 200
    order = client.get(f"/api/runs/{rid}").get_json()["order_reviews"]["OM"]
    assert order["status"] == "READY"
    assert order["order_id"] == "OM"


def test_issue_all_only_issues_ready_orders(client):
    run = create_with_rows(client, [
        row(order_id="OK1", line_id="OK1L1", payment="10.00"),
        row(order_id="BAD1", line_id="BAD1L1", line_amount="nope"),
        row(order_id="OK2", line_id="OK2L1", payment="10.00"),
    ])
    rid = run["run_id"]
    response = client.post(f"/api/runs/{rid}/issue-all", json={})
    assert response.status_code == 200
    body = response.get_json()
    assert set(body["issued_orders"]) == {"OK1", "OK2"}
    assert body["order_reviews"]["BAD1"]["status"] == "HELD"


def test_dirty_sample_board_attributes_known_orders_without_batch_hold(client):
    run = client.post("/api/runs", json={"profile": "dirty"}).get_json()
    board = run["review_board"]
    assert board["batch_hold"] is False
    held = {o["order_id"] for o in board["orders"] if o["status"] == "HELD"}
    # O5 冲突重复、O6 退货数量、O8 负金额、O1 列数异常都可认出归属
    assert {"O1", "O5", "O6", "O8"} <= held
