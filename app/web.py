from flask import render_template,request
from app import store
from app.pipeline import run
from app.config import TITLE,MODULES,STAGES,COLUMNS,SAMPLES,DEFAULTS
from app.modules.m8_backfill.rules import summarize_orders, plan_backfill
def view(stage):
    if not 1<=stage<=8: return "页面不存在",404
    run_id=request.args.get("run")
    result=store.fetch(run_id) if run_id else run()
    sample=(SAMPLES/"dirty.csv").read_text(encoding="utf-8-sig")
    data=result["stages"][stage-1]
    backfills=result.get("backfills",[])
    sold={t['seat'] for t in result['tickets']};paid={t['seat'] for t in result['payments']};held={t['seat'] for t in result['leases']}
    fill={b['new_seat'] for b in backfills}
    seats=[{'seat':f'A-{i:02d}','state':'补位出票' if f'A-{i:02d}' in fill else '已出票' if f'A-{i:02d}' in sold else '待出票' if f'A-{i:02d}' in paid else '已过期' if f'A-{i:02d}' in held else '空闲'} for i in range(1,17)]
    # 页面始终按最新原票与补位结果重算订单状态，避免补位后展示旧版结论。
    orders=summarize_orders(result['leases'],result['payments'],result['tickets'],backfills,result['quarantine'])
    plans,_=plan_backfill(orders,result['leases'],result['payments'],result['tickets'],backfills,result['quarantine'])
    return render_template(f"m{stage}.html",title=TITLE,modules=MODULES,stage=stage,result=result,data=data,
                           sample=sample,defaults=DEFAULTS,columns=COLUMNS,history=store.history(),seats=seats,
                           orders=orders,backfill_plans=plans)
