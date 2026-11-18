import pytest
from decimal import Decimal as D
from app.pipeline import run
from app.config import SAMPLES
from app.errors import DomainError
from app.normalize import instant
from app.modules.m8_backfill.rules import summarize_orders, plan_backfill, issue_backfill, CATALOG

GROUP = (SAMPLES / 'group.csv').read_bytes()
END = instant('2026-11-18T00:00:00+08:00')


def _ctx(raw=GROUP):
    return run(raw, stop=8, persist=False)


def test_group_order_partial_is_not_complete():
    ctx = _ctx()
    g100 = next(o for o in ctx['orders'] if o['order'] == 'G100')
    # 2 座订单只出 1 张原票：A-02 有效支付但票号缺失可补位，不能显示完成。
    assert g100['state'] == '部分出票'
    assert g100['expected'] == 2 and g100['issued'] == 1 and g100['missing'] == 1
    assert g100['missing_seats'] == ['A-02']
    g200 = next(o for o in ctx['orders'] if o['order'] == 'G200')
    assert g200['state'] == '已完成'
    # G400 在到期点付款（无效），没有有效支付不可补位，订单保持未完成。
    g400 = next(o for o in ctx['orders'] if o['order'] == 'G400')
    assert g400['state'] == '待补位' and g400['blocked_seats'] == ['A-09']


def test_original_ticket_receipts_kept_unchanged():
    ctx = _ctx()
    original = {t['ticket'] for t in ctx['tickets']}
    assert original == {'10000001', '10000004', '10000005', '10000008'}
    assert all(t.get('source', '原票') != '补位' for t in ctx['tickets'])
    # 失败的渠道回执仍保留在隔离区，原因码与行号可追溯。
    assert {q['raw']['id']: q['code'] for q in ctx['quarantine']} == {
        'G1I2': 'MISSING', 'G4P1': 'EXPIRED', 'G4I1': 'ORPHAN'}


def test_backfill_plans_only_missing_seats():
    ctx = _ctx()
    plans = ctx['backfill_plans']
    # 仅 A-02 有有效支付且缺票；A-03 支付过期不在补偿范围。
    assert [p['old_seat'] for p in plans] == ['A-02']
    plan = plans[0]
    assert plan['order'] == 'G100' and plan['channel'] == '团购' and D(plan['amount']) == D(100)
    assert plan['evidence_code'] == 'MISSING'
    # 已出票座席与全部占用租约都不会被选为补位目标。
    assert plan['new_seat'] not in {t['seat'] for t in ctx['tickets']}
    assert plan['new_seat'] not in {l['seat'] for l in ctx['leases']}


def test_execute_backfill_completes_order_and_is_idempotent(client):
    saved = client.post('/api/runs', json={'text': GROUP.decode()}).json
    run_id = saved['manifest']['run_id']
    url = f'/api/backfill/{run_id}'
    first = client.post(url, json={'version': saved['version']}).json
    assert first['added'] == 1 and first['tickets'][0]['old_seat'] == 'A-02'
    assert first['tickets'][0]['source'] == '补位'
    after = first['run']
    g100 = next(o for o in after['orders'] if o['order'] == 'G100')
    assert g100['state'] == '已完成' and g100['backfilled'] == 1
    # 重跑：缺失座位已补齐，不重复出票，版本不推进。
    second = client.post(url, json={'version': after['version']}).json
    assert second['added'] == 0 and second['run']['version'] == after['version']
    assert len(second['run']['backfills']) == 1


def test_no_duplicate_voucher_after_rerun(client):
    saved = client.post('/api/runs', json={'text': GROUP.decode()}).json
    run_id = saved['manifest']['run_id']
    url = f'/api/backfill/{run_id}'
    first = client.post(url, json={'version': saved['version']}).json
    second = client.post(url, json={'version': first['run']['version']}).json
    tickets = [b['ticket'] for b in second['run']['backfills']]
    assert len(tickets) == len(set(tickets)) == 1
    # 补位票号不得与原票撞号。
    assert tickets[0] not in {t['ticket'] for t in second['run']['tickets']}


def test_stale_version_keeps_old_state(client):
    saved = client.post('/api/runs', json={'text': GROUP.decode()}).json
    run_id = saved['manifest']['run_id']
    url = f'/api/backfill/{run_id}'
    ok = client.post(url, json={'version': saved['version']}).json
    stale = client.post(url, json={'version': saved['version']})
    assert stale.status_code == 409 and stale.json['code'] == 'STALE'
    # 失败后旧版状态保留：补位结果仍是第一次的一张。
    kept = client.get(f'/api/runs/{run_id}').json
    assert kept['version'] == ok['run']['version']
    assert len(kept['backfills']) == 1


def test_requested_seat_is_used(client):
    saved = client.post('/api/runs', json={'text': GROUP.decode()}).json
    reply = client.post(f"/api/backfill/{saved['manifest']['run_id']}",
                        json={'version': saved['version'], 'seats': ['A-12']}).json
    assert reply['tickets'][0]['new_seat'] == 'A-12'


def test_requested_seat_count_must_match_demand(client):
    saved = client.post('/api/runs', json={'text': GROUP.decode()}).json
    # G100 只有 1 个可补座位，传 2 个座席必须拒绝且不写库。
    reply = client.post(f"/api/backfill/{saved['manifest']['run_id']}",
                        json={'version': saved['version'], 'seats': ['A-06', 'A-07']})
    assert reply.status_code == 422 and reply.json['code'] == 'LIMIT'
    kept = client.get(f"/api/runs/{saved['manifest']['run_id']}").json
    assert kept.get('backfills', []) == []


def test_auto_target_never_takes_blocked_order_seat():
    ctx = _ctx()
    blocked = {s for o in ctx['orders'] for s in o['blocked_seats']}
    assert blocked == {'A-09'}
    for plan in ctx['backfill_plans']:
        assert plan['new_seat'] not in blocked
        assert plan['new_seat'] not in {l['seat'] for l in ctx['leases']}


def test_requested_seat_sold_rejected(client):
    saved = client.post('/api/runs', json={'text': GROUP.decode()}).json
    reply = client.post(f"/api/backfill/{saved['manifest']['run_id']}",
                        json={'version': saved['version'], 'seats': ['A-01']})
    assert reply.status_code == 422 and reply.json['code'] == 'OVERLAP'
    kept = client.get(f"/api/runs/{saved['manifest']['run_id']}").json
    assert kept.get('backfills', []) == []


def test_export_cross_checks_original_backfill_and_order(client):
    saved = client.post('/api/runs', json={'text': GROUP.decode()}).json
    run_id = saved['manifest']['run_id']
    client.post(f'/api/backfill/{run_id}', json={'version': saved['version']})
    csv_text = client.get(f'/api/backfill.csv?run={run_id}').text
    assert '原票（保留渠道回执）' in csv_text
    assert '补位凭证（同一订单记录）' in csv_text
    assert '订单完成状态核对' in csv_text
    assert 'G100' in csv_text and 'BF00000001' in csv_text and '已完成' in csv_text


def test_m8_page_renders(client):
    saved = client.post('/api/runs', json={'text': GROUP.decode()}).json
    page = client.get(f"/m/8?run={saved['manifest']['run_id']}")
    assert page.status_code == 200 and '订单完成状态' in page.text


def test_no_free_seat_limit():
    ctx = _ctx()
    # 用额外租约占满全部空闲座席后再请求补位：动作阶段直接拒绝，不落任何补位凭证。
    held = {l['seat'] for l in ctx['leases']}
    extra = [dict(ctx['leases'][0], lease=f'X{i}', seat=s, order=f'X{i}')
             for i, s in enumerate(s for s in CATALOG if s not in held)]
    with pytest.raises(DomainError) as exc:
        plan_backfill(ctx['orders'], ctx['leases'] + extra, ctx['payments'], ctx['tickets'], [],
                      ctx['quarantine'], requested=['A-06'])
    assert exc.value.code in ('OVERLAP', 'LIMIT')


def test_ticket_number_uniqueness_against_existing():
    ctx = _ctx()
    plans, used = plan_backfill(ctx['orders'], ctx['leases'], ctx['payments'],
                                ctx['tickets'], [], ctx['quarantine'])
    issued = issue_backfill(plans, used, END)
    assert issued[0]['ticket'] not in {t['ticket'] for t in ctx['tickets']}


def test_released_lease_is_not_expected():
    ctx = _ctx()
    leases = [dict(l) for l in ctx['leases']]
    for lease in leases:
        if lease['order'] == 'G200':
            lease['released'] = True
            lease['state'] = '已释放'
    orders = summarize_orders(leases, ctx['payments'], ctx['tickets'], [], ctx['quarantine'])
    # G200 主动释放后不再计入应出票；其余订单仍在集合内。
    assert all(o['order'] != 'G200' for o in orders)
    assert {o['order'] for o in orders} == {'G100', 'G300', 'G400'}
