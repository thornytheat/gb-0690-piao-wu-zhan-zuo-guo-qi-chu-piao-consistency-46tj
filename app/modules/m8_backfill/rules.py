from datetime import timezone
from app.errors import DomainError

CATALOG = [f'A-{i:02d}' for i in range(1, 17)]


def _effective_leases(leases):
    # 主动释放的座席不算订单应出票范围；过期与占用中的租约都需要凭证。
    return [lease for lease in leases if not lease.get('released')]


def _evidence_for(quarantine, order, seat):
    for code in ('ORPHAN', 'CONFLICT', 'MISSING', 'EXPIRED'):
        for item in quarantine:
            raw = item.get('raw', {})
            if item.get('code') == code and raw.get('order') == order and raw.get('seat') == seat:
                return {'line': item.get('line', ''), 'code': code, 'detail': item.get('detail', '')}
    return None


# 有有效支付时，票号缺失/冲突仍可补位；只有缺少支付或租约失效才永久受阻。
BLOCKING_CODES = ('ORPHAN', 'EXPIRED')


def summarize_orders(leases, payments, tickets, backfills, quarantine):
    """同一订单聚合：应出票座位、原渠道票、补位票与完成状态。部分出票不得显示完成。"""
    expected = {}
    for lease in _effective_leases(leases):
        expected.setdefault(lease['order'], []).append(lease['seat'])
    paid = {(p['order'], p['seat']) for p in payments}
    orders = []
    for order in sorted(expected):
        seats = sorted(set(expected[order]))
        original = sorted(t['seat'] for t in tickets if t['order'] == order)
        order_fills = [b for b in backfills if b['order'] == order]
        filled = sorted(b['new_seat'] for b in order_fills)
        # 补位凭证补偿的是旧座席的缺票事实，订单完成度按旧座席核对。
        covered = set(original) | {b['old_seat'] for b in order_fills} | set(filled)
        missing = [seat for seat in seats if seat not in covered]
        blocked = []
        for seat in missing:
            evidence = _evidence_for(quarantine, order, seat)
            if (order, seat) not in paid or (evidence and evidence['code'] in BLOCKING_CODES):
                blocked.append(seat)
        outstanding = [seat for seat in missing if seat not in blocked]
        state = '已完成' if not missing else '部分出票' if original or filled else '待补位'
        orders.append({'order': order, 'expected': len(seats), 'issued': len(original),
                       'backfilled': len(filled), 'missing': len(missing),
                       'seats': seats, 'original_seats': original, 'backfill_seats': filled,
                       'missing_seats': outstanding, 'blocked_seats': blocked, 'state': state})
    return orders


def _ticket_no(index):
    return f'BF{index:08d}'


def plan_backfill(orders, leases, payments, tickets, backfills, quarantine, requested=None):
    """只补偿缺失座位；已出票与已补位座席不再产生计划。"""
    used_tickets = {t['ticket'] for t in tickets} | {b['ticket'] for b in backfills}
    sold_seats = {t['seat'] for t in tickets} | {b['new_seat'] for b in backfills}
    # 已成功补位的旧座席视为腾空：其租约已失效且同一订单缺失座位已补齐，重跑不再占用空闲容量。
    vacated = {b['old_seat'] for b in backfills}
    held_seats = {l['seat'] for l in leases if not l.get('released')} - vacated
    blocked_seats = {seat for order in orders for seat in order['blocked_seats']}
    # 受阻订单的租约座席绝不能自动分配给其他订单，避免把未结清订单的座席换走。
    free_seats = [seat for seat in CATALOG
                  if seat not in sold_seats and seat not in held_seats and seat not in blocked_seats]
    if requested:
        unknown = [seat for seat in requested if seat not in CATALOG]
        if unknown:
            raise DomainError('UNMAPPED', f"补位座席不在演出目录: {','.join(unknown)}")
        occupied = [seat for seat in requested if seat in sold_seats or seat in held_seats or seat in blocked_seats]
        if occupied:
            raise DomainError('OVERLAP', f"补位座席已被占用或出票: {','.join(occupied)}")
    demands = []
    for order in orders:
        for seat in order['missing_seats']:
            lease = next(l for l in _effective_leases(leases)
                         if l['order'] == order['order'] and l['seat'] == seat)
            pay = next((p for p in payments if p['order'] == order['order'] and p['seat'] == seat), None)
            if pay is None:
                continue
            evidence = _evidence_for(quarantine, order['order'], seat)
            demands.append({'order': order['order'], 'old_seat': seat, 'channel': lease['channel'],
                            'amount': pay['amount'], 'lease': lease['lease'],
                            'payment': pay['id'], 'evidence_line': evidence['line'] if evidence else '',
                            'evidence_code': evidence['code'] if evidence else ''})
    targets = list(requested) if requested else free_seats
    if requested:
        if len(targets) != len(demands):
            raise DomainError('LIMIT', f"指定座席数 {len(targets)} 与待补座位数 {len(demands)} 不一致")
    # 自动分配按当前空闲容量截断；仍未覆盖的缺失座位保留在订单状态里，下次恢复再补。
    plans = []
    for index, demand in enumerate(demands):
        if index >= len(targets):
            break
        plans.append({**demand, 'new_seat': targets[index]})
    return plans, used_tickets


def issue_backfill(plans, used_tickets, observation_end):
    """依据计划生成补位凭证；票号全场唯一，重跑时同一订单缺失座位已补齐则不会再生成。"""
    issued = []
    stamp = observation_end.astimezone(timezone.utc)
    for index, plan in enumerate(plans, start=1):
        ticket = _ticket_no(index)
        suffix = 1
        while ticket in used_tickets:
            suffix += 1
            ticket = f'BF{index:08d}-{suffix}'
        used_tickets.add(ticket)
        issued.append({'id': f'BF-{plan["order"]}-{plan["old_seat"]}', 'order': plan['order'],
                       'channel': plan['channel'], 'lease': plan['lease'], 'payment': plan['payment'],
                       'old_seat': plan['old_seat'], 'new_seat': plan['new_seat'],
                       'amount': plan['amount'], 'ticket': ticket, 'source': '补位',
                       'evidence_line': plan['evidence_line'], 'evidence_code': plan['evidence_code'],
                       'time': stamp, 'state': '补位已出票'})
    return issued
