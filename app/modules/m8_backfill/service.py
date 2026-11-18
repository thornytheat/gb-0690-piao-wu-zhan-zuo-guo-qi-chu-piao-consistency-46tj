from .rules import summarize_orders, plan_backfill


def _refresh(ctx):
    # 派生集合始终从同一批次的原票、租约、支付和既有补位结果重算，保证页面核对口径一致。
    ctx['orders'] = summarize_orders(ctx['leases'], ctx['payments'], ctx['tickets'],
                                     ctx.get('backfills', []), ctx['quarantine'])
    plans, used_tickets = plan_backfill(ctx['orders'], ctx['leases'], ctx['payments'],
                                        ctx['tickets'], ctx.get('backfills', []),
                                        ctx['quarantine'])
    ctx['backfill_plans'] = plans
    return plans, used_tickets


def execute(ctx):
    ctx.setdefault('backfills', [])
    plans, _ = _refresh(ctx)
    done = sum(1 for order in ctx['orders'] if order['state'] == '已完成')
    partial = sum(1 for order in ctx['orders'] if order['state'] == '部分出票')
    ctx['stages'].append({'title': '团购补位', 'rows': ctx['orders'],
                          'metrics': {'订单': len(ctx['orders']), '已完成': done, '部分出票': partial,
                                      '补位凭证': len(ctx['backfills']), '待补位座位': len(plans)}})
    return ctx
