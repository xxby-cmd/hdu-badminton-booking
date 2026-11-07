"""Explicit, gated entry point; no live operation is the default."""
import argparse
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from datetime import date

from booking import Api, BookingError, Option, Plan, PriceError, ServerClock, TZ, candidates
from cancel_budget import CancellationBudget
from once_journal import OnceTestJournal, TestAlreadyUsed
from simulation import ROOT, require_current_simulation, run as simulate
from single_test import SingleTestFlow

PRIVATE = ROOT / '.private'
JOURNAL = PRIVATE / 'tennis-live-test-20261103.json'
EVIDENCE = PRIVATE / 'cancellation-evidence.json'


class NoFreeTennisSlot(BookingError):
    pass


def cancellation_evidence():
    try:
        evidence = json.loads(EVIDENCE.read_text(encoding='utf-8'))
        budget = CancellationBudget(
            evidence['used_count'], evidence['restriction_at'], evidence['period'],
            evidence['source'], datetime.fromisoformat(evidence['observed_at']),
            evidence.get('rule_confirmed') is True, evidence.get('count_confirmed') is True)
        budget.ensure_one_safe_cancellation()
        if evidence.get('manual_invitation_ready') is not True:
            raise BookingError('需要本人准备手动邀请同伴，之后才可测试取消')
        if not isinstance(evidence.get('account_fingerprint'), str):
            raise BookingError('取消次数资料没有绑定本次账号')
        return budget, evidence['account_fingerprint']
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise BookingError('取消规则、当前次数或取消条件尚未核实，禁止真实测试') from exc


def choose_one_free_slot(api, day, server_day):
    """Choose two consecutive one-hour periods in one free order."""
    reply = api.call('choose_venue', {'venue_type': 'tennis'})
    venues = reply.data.get('venue_list')
    if reply.status != 200 or not isinstance(venues, list):
        raise BookingError('网球场馆列表无法确认，不开始真实测试')
    for item in venues:
        if not isinstance(item, dict) or not isinstance(item.get('venue_name'), str):
            raise BookingError('网球场馆信息格式变化，不开始真实测试')
        venue = item['venue_name']
        meta_reply = api.call('post_venue_info', {'date': day, 'venue_name': venue})
        if meta_reply.status != 200:
            raise BookingError('网球时段查询失败，不开始真实测试')
        meta = meta_reply.data
        days_forward = (date.fromisoformat(day) - date.fromisoformat(server_day)).days
        if type(meta.get('days_can_appoint')) is not int:
            raise BookingError('开放预约天数格式无法确认')
        if not 1 <= days_forward < meta['days_can_appoint']:
            continue
        availability_reply = api.call('post_site_situation', {
            'date': day, 'venue_name': venue, 'is_permanent': False})
        if availability_reply.status != 200:
            raise BookingError('网球空位查询失败，不开始真实测试')
        times = meta.get('time_list')
        if not isinstance(times, list) or len(times) < 3:
            raise BookingError('网球时段表无法确认')
        for start, middle, end in zip(times, times[1:], times[2:]):
            try:
                marks = [datetime.strptime(value, '%H:%M') for value in (start, middle, end)]
                durations = [(right - left).total_seconds() for left, right in zip(marks, marks[1:])]
            except (ValueError, TypeError) as exc:
                raise BookingError('网球时段格式变化') from exc
            if durations != [3600, 3600]:
                continue
            plan = replace(Plan(), day=day, venue=venue, venue_type='tennis',
                           start=start, end=end, max_price=0, allow_one_hour=False)
            try:
                courts = candidates(meta, availability_reply.data, plan)
            except PriceError:
                continue
            if courts:
                site, total = courts[0]
                return plan, meta, Option(site, total, plan)
    raise NoFreeTennisSlot('该日期没有确认连续两个可用的免费网球时段，未开始真实测试')


def live_once():
    require_current_simulation()
    if JOURNAL.exists():
        raise TestAlreadyUsed('本次真实测试已经登记，只能核对已有结果，禁止重跑')
    budget, account = cancellation_evidence()
    from session import PRIVATE as session_private, load_session
    if session_private.resolve() != PRIVATE.resolve():
        raise BookingError('真实测试禁止更换本机状态目录')
    session, _ = load_session()
    if hashlib.sha256(str(session['openid']).encode()).hexdigest() != account:
        raise BookingError('取消次数资料与当前账号不一致')
    api = Api(session)
    try:
        clock = ServerClock()
        clock.sync(api)
        server_date = datetime.fromtimestamp(clock.now(), TZ).date()
        for offset in (1, 2):
            day = (server_date + timedelta(days=offset)).isoformat()
            try:
                plan, meta, option = choose_one_free_slot(api, day, server_date.isoformat())
                break
            except NoFreeTennisSlot:
                continue
        else:
            raise NoFreeTennisSlot('可预约日期内没有连续两个免费网球时段，未开始真实测试')
        # This is a new immediate functional test, not a replay of the 20:00 release.
        plan = replace(plan, release=datetime.fromtimestamp(clock.now(), TZ))
        option = Option(option.site, option.total, plan)
        result = SingleTestFlow(api, OnceTestJournal(JOURNAL), plan, meta, option,
                                budget, clock=clock, publish=lambda **event:
                                print(json.dumps(event, ensure_ascii=False), flush=True)).run()
        result['peak_timing_test'] = False
        return result
    finally:
        if api.connection:
            api.connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live-once', action='store_true', help='仿真和取消条件全部核实后，只执行一次真实测试')
    args = parser.parse_args()
    if not args.live_once:
        return 0 if simulate() else 1
    try:
        print(json.dumps(live_once(), ensure_ascii=False))
        return 0
    except BookingError as exc:
        print(json.dumps({'state': 'STOPPED', 'message': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
