import csv, io
from flask import Blueprint, request, jsonify, Response
from app.pipeline import run
from app import store
from app.errors import DomainError
from app.normalize import instant
from app.modules.m8_backfill.rules import summarize_orders, plan_backfill, issue_backfill
from app.modules.m8_backfill.service import _refresh

bp = Blueprint("m8", __name__)


def _request_text(payload):
    raw = payload.get("text")
    if raw is not None and not isinstance(raw, str):
        raise DomainError("TEXT", "数据必须为文本")
    return raw


@bp.route("/api/m8", methods=["GET", "POST"])
def endpoint():
    payload = request.get_json(silent=True) if request.method == "POST" else {}
    if not isinstance(payload, dict):
        raise DomainError("BODY", "请求必须是对象")
    return jsonify(run(_request_text(payload), payload.get("params"), stop=8, persist=False))


def _hydrate(result):
    ctx = dict(result)
    if isinstance(ctx['observation_end'], str):
        ctx['observation_end'] = instant(ctx['observation_end'])
    plans, used_tickets = _refresh(ctx)
    return ctx, plans, used_tickets


@bp.route("/api/backfill/<run_id>", methods=["POST"])
def backfill(run_id):
    payload = request.get_json(silent=True) or {}
    version = payload.get("version")
    if not isinstance(version, int):
        raise DomainError("STALE", "版本必须为整数")
    requested = payload.get("seats")
    if requested is not None and (not isinstance(requested, list)
                                  or not all(isinstance(x, str) for x in requested)):
        raise DomainError("BODY", "补位座席必须为字符串数组")
    result = store.fetch(run_id)
    ctx, plans, used_tickets = _hydrate(result)
    end = instant(ctx['observation_end']) if isinstance(ctx['observation_end'], str) else ctx['observation_end']
    if requested is not None:
        plans, used_tickets = plan_backfill(ctx['orders'], ctx['leases'], ctx['payments'],
                                            ctx['tickets'], ctx.get('backfills', []),
                                            ctx['quarantine'], requested)
    new_tickets = issue_backfill(plans, used_tickets, end)
    merged = ctx.get('backfills', []) + new_tickets
    orders = summarize_orders(ctx['leases'], ctx['payments'], ctx['tickets'], merged, ctx['quarantine'])
    remaining, _ = plan_backfill(orders, ctx['leases'], ctx['payments'], ctx['tickets'],
                                 merged, ctx['quarantine'])
    saved, added = store.backfill(run_id, version, new_tickets, orders, remaining)
    return jsonify({'run': saved, 'added': len(added), 'tickets': new_tickets})


@bp.route("/api/backfill.csv")
def export():
    run_id = request.args.get("run")
    result = store.fetch(run_id) if run_id else run()
    ctx, plans, _ = _hydrate(result)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    header = ['订单', '凭证来源', '票号', '原座席', '补位座席', '渠道', '金额 CNY', '订单完成状态', '依据']
    writer.writerow(['# 原票（保留渠道回执）'])
    writer.writerow(header)
    for ticket in ctx['tickets']:
        order = next((o for o in ctx['orders'] if o['order'] == ticket['order']), None)
        writer.writerow([ticket['order'], '原票', ticket['ticket'], ticket['seat'], '',
                         ticket['channel'], str(ticket['amount']),
                         order['state'] if order else '', f"行 {ticket.get('_line','')} 渠道回执"])
    writer.writerow([])
    writer.writerow(['# 补位凭证（同一订单记录）'])
    writer.writerow(header)
    for item in ctx.get('backfills', []):
        order = next(o for o in ctx['orders'] if o['order'] == item['order'])
        reason = f"{item['evidence_code']} 行{item['evidence_line']}" if item['evidence_line'] else '租约到期缺票'
        writer.writerow([item['order'], '补位', item['ticket'], item['old_seat'], item['new_seat'],
                         item['channel'], str(item['amount']), order['state'], reason])
    writer.writerow([])
    writer.writerow(['# 订单完成状态核对（部分出票不得显示完成）'])
    writer.writerow(['订单', '应出票', '原票', '补位', '缺失', '状态', '缺失座席', '受阻座席'])
    for order in ctx['orders']:
        writer.writerow([order['order'], order['expected'], order['issued'], order['backfilled'],
                         order['missing'], order['state'], '|'.join(order['missing_seats']),
                         '|'.join(order['blocked_seats'])])
    return Response("﻿" + stream.getvalue(), content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=backfill.csv"})
