"""Cost-only reporting regression checks; no calls or external providers."""
from decimal import Decimal
from test_ingestion import make_event, post_events


def test_multiple_events_do_not_multiply_call_count_or_duration(client, db_connection):
    call_id = 'pytest-monitor-one-call'
    events = [
        make_event('pytest-monitor-llm1', call_id, input_tokens=1000, cached_input_tokens=200, output_tokens=500),
        make_event('pytest-monitor-llm2', call_id, input_tokens=100, cached_input_tokens=20, output_tokens=50),
        make_event('pytest-monitor-voice', call_id, provider='plivo', model='voice', stage='telephony',
                   billing_unit='minute', quantity=2, input_tokens=0, output_tokens=0, cached_input_tokens=0),
    ]
    response = post_events(client, events)
    assert response.status_code == 200
    assert response.json()['inserted'] == 3
    with db_connection.cursor() as cur:
        cur.execute('UPDATE calls SET total_duration_s=60 WHERE call_id=%s', (call_id,))
    db_connection.commit()
    day = events[0]['occurred_at'][:10]
    body = client.get('/internal/costs/breakdown', params={'start_date': day, 'end_date': day}).json()
    summary = body['summary']
    assert summary['call_count'] == 1
    assert Decimal(body['periods'][0]['total_duration_s']) == 60
    assert Decimal(summary['total_billed_minutes']) == 2
    assert summary['input_tokens'] == 1100
    assert summary['cached_input_tokens'] == 220
    assert summary['output_tokens'] == 550
    assert summary['total_tokens'] == 1650
    assert Decimal(summary['variable_cost_usd']) < Decimal(summary['total_cost_usd'])
    assert Decimal(summary['llm_cost_usd']) == Decimal(summary['openai_cost_usd'])
    calls = client.get('/internal/calls', params={'provider': 'llm'}).json()['calls']
    assert any(c['call_id'] == call_id and Decimal(c['billed_minutes']) == 2 for c in calls)


def test_fixed_subscription_prorates_without_changing_daily_contract(client, monkeypatch):
    import main
    monkeypatch.setattr(main, 'FIXED_ELEVENLABS_PLAN_MONTHLY_USD', Decimal('299'))
    body = client.get('/internal/costs/breakdown', params={'start_date': '2026-10-01', 'end_date': '2026-10-31'}).json()
    assert Decimal(body['summary']['fixed_elevenlabs_plan_usd']) == Decimal('299.000000') or abs(Decimal(body['summary']['fixed_elevenlabs_plan_usd'])-299) < Decimal('0.00002')
    assert Decimal(body['summary']['variable_cost_usd']) == 0
    daily = client.get('/internal/costs/daily', params={'day': '2026-10-01'}).json()
    assert Decimal(daily['total_cost_usd']) == Decimal(daily['variable_cost_usd']) + Decimal(daily['fixed_cost_usd'])
