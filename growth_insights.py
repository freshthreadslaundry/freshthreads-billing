"""Read-only, identifier-free growth aggregates for the manager dashboard."""
from collections import defaultdict
import re
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from billing_math import totals, money


def growth_insights(conn, today):
    current_start = today - timedelta(days=14)
    previous_start = today - timedelta(days=29)
    previous_end = current_start - timedelta(days=1)
    raw = conn.execute("SELECT * FROM bills WHERE IFNULL(void,0)=0").fetchall()
    items = defaultdict(list)
    for row in conn.execute("SELECT i.bill_id, i.service_name, i.total, s.name AS canonical_name FROM bill_items i LEFT JOIN services s ON s.id=i.service_id"):
        line = dict(row)
        line['service_name'] = line.pop('canonical_name') or re.sub(r' \([0-9]+(?:\.[0-9]+)? KG\)$', '', line['service_name'] or '', flags=re.I)
        items[row['bill_id']].append(line)
    rows, invalid_dates, invalid_amounts = [], 0, 0
    for raw_row in raw:
        r = dict(raw_row)
        try:
            day = date.fromisoformat(str(r['date'])[:10])
        except (ValueError, TypeError):
            invalid_dates += 1
            continue
        if day > today:
            continue
        r['day'] = day
        # Older bills are needed for customer history, not period sales.
        r['sales'] = None
        if day >= previous_start:
            try:
                r['sales'] = money(totals({**r, 'advance_paid': 0}, items[r['id']])['total_before_wallet'])
            except (ValueError, ArithmeticError):
                invalid_amounts += 1
        r['channel'] = {'Online': 'Online', 'Walk-in': 'Walk-in'}.get(r['order_type'], 'Other / missing')
        rows.append(r)
    history = defaultdict(list)
    for r in sorted(rows, key=lambda x: (x['day'], str(x['date']), x['id'])):
        history[r['customer_id']].append(r)

    def period(start, end):
        selected = [r for r in rows if start <= r['day'] <= end]
        channels = []
        for channel in ('Online', 'Walk-in', 'Other / missing'):
            subset = [r for r in selected if r['channel'] == channel]
            value = sum((r['sales'] for r in subset if r['sales'] is not None), Decimal(0))
            channels.append(dict(name=channel, count=len(subset), sales=value,
                                 average=value/sum(r['sales'] is not None for r in subset) if any(r['sales'] is not None for r in subset) else Decimal(0)))
        cohorts = []
        for name in ('New', 'Repeat'):
            subset = [r for r in selected if ('New' if history[r['customer_id']][0]['day'] >= start else 'Repeat') == name]
            cohorts.append(dict(name=name, customers=len({r['customer_id'] for r in subset}),
                                sales=sum((r['sales'] for r in subset if r['sales'] is not None), Decimal(0))))
        service = defaultdict(lambda: Decimal(0))
        for r in selected:
            if r['sales'] is None:
                continue
            lines = items[r['id']]
            subtotal = sum((money(i['total']) for i in lines), Decimal(0))
            # Reconcile allocated discounts/express charges exactly to each bill, down to paise.
            remaining = r['sales']
            for idx, line in enumerate(lines):
                allocation = remaining if idx == len(lines)-1 else (r['sales'] * money(line['total']) / subtotal).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) if subtotal else Decimal(0)
                allocation = min(remaining, allocation)
                service[line['service_name'] or 'Unspecified service'] += allocation
                remaining -= allocation
            if not lines:
                service['Unspecified service'] += remaining
        weekdays = []
        for weekday, name in enumerate(('Mon','Tue','Wed','Thu','Fri','Sat','Sun')):
            days = sum((start + timedelta(days=i)).weekday() == weekday for i in range(15))
            count = sum(r['day'].weekday() == weekday for r in selected)
            weekdays.append(dict(name=name, count=count, average=count/days))
        return dict(start=start, end=end, count=len(selected), sales=sum((r['sales'] for r in selected if r['sales'] is not None),Decimal(0)),
                    channels=channels, cohorts=cohorts, services=dict(service), weekdays=weekdays)
    current, previous = period(current_start,today), period(previous_start,previous_end)
    daily = []
    for i in range(30):
        day = previous_start + timedelta(days=i)
        daily.append(dict(day=day, **{channel: sum((r['sales'] for r in rows if r['day']==day and r['channel']==channel and r['sales'] is not None),Decimal(0)) for channel in ('Online','Walk-in','Other / missing')}))
    maximum = max([float(d[c]) for d in daily for c in ('Online','Walk-in','Other / missing')] + [1])
    lines = []
    for channel, color in [('Online','#2563eb'),('Walk-in','#0d9488'),('Other / missing','#a16207')]:
        if channel == 'Other / missing' and not any(d[channel] for d in daily):
            continue
        points = ' '.join(f'{60+i*26:.1f},{210-float(d[channel])/maximum*170:.1f}' for i,d in enumerate(daily))
        lines.append(dict(name=channel,color=color,points=points))
    services = sorted(set(current['services']) | set(previous['services']),key=lambda name: current['services'].get(name,0)+previous['services'].get(name,0),reverse=True)
    service_rows = [dict(name=name,current=current['services'].get(name,Decimal(0)),previous=previous['services'].get(name,Decimal(0))) for name in services[:5]]
    if len(services)>5:
        service_rows.append(dict(name='Other services',current=sum((current['services'].get(n,0) for n in services[5:]),Decimal(0)),previous=sum((previous['services'].get(n,0) for n in services[5:]),Decimal(0))))
    max_service = max([float(r[k]) for r in service_rows for k in ('current','previous')]+[1])
    for r in service_rows:
        r['current_width'] = float(r['current'])/max_service*100
        r['previous_width'] = float(r['previous'])/max_service*100
    for p in (current,previous):
        total_customers = sum(c['customers'] for c in p['cohorts'])
        for cohort in p['cohorts']:
            cohort['share'] = cohort['customers']/total_customers*100 if total_customers else 0
            cohort['sales_share'] = float(cohort['sales']/p['sales']*100) if p['sales'] else 0
    def retention(start,end):
        eligible = [orders for orders in history.values() if start <= orders[0]['day'] <= end]
        returned = sum(len(orders)>1 and orders[1]['day'] <= orders[0]['day']+timedelta(days=15) for orders in eligible)
        return dict(start=start,end=end,total=len(eligible),returned=returned,rate=returned/len(eligible)*100 if eligible else None)
    return dict(current=current,previous=previous,daily=daily,lines=lines,max_axis=maximum,
                services=service_rows,invalid_dates=invalid_dates,invalid_amounts=invalid_amounts,
                sales_change=(float((current['sales']-previous['sales'])/previous['sales']*100) if previous['sales'] else None),
                retention_current=retention(today-timedelta(days=29),today-timedelta(days=15)),
                retention_previous=retention(today-timedelta(days=44),today-timedelta(days=30)))
