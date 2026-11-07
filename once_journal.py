"""Durable one-test allowance; cancellation never replenishes the allowance."""
from datetime import datetime
import json
import os
from pathlib import Path
import threading

from booking import BookingError, TZ, checked_price, exact_order
from cancel_budget import CancellationBudget


class TestAlreadyUsed(BookingError):
    pass


class OnceTestJournal:
    ENDPOINTS = frozenset({'creat_book_info', 'creat_order', 'cancel_order'})

    def __init__(self, path):
        self.path = Path(path)
        self.data = None
        self._owner = False
        self._guard = threading.Lock()

    def claim(self, plan, site, baseline_order_numbers=(), cancellation_budget=None):
        if not isinstance(cancellation_budget, CancellationBudget):
            raise BookingError('必须先确认本次取消不会触发限制')
        budget = cancellation_budget.ensure_one_safe_cancellation()
        if plan.venue_type != 'tennis' or plan.max_price != 0:
            raise BookingError('单次测试只允许已选定的免费网球场计划')
        checked_price(0, plan.max_price)
        if type(site) is not int or site < 1:
            raise BookingError('单次测试场地无效')
        data = {
            'version': 1, 'purpose': 'one-tennis-book-and-cancel',
            'claimed_at': datetime.now(TZ).isoformat(), 'phase': 'CLAIMED',
            'plan': {'day': plan.day, 'venue': plan.venue,
                     'venue_type': plan.venue_type, 'start': plan.start,
                     'end': plan.end, 'site': site, 'max_price': 0},
            'baseline_order_numbers': [str(value) for value in baseline_order_numbers],
            'requests': {endpoint: 0 for endpoint in self.ENDPOINTS},
            'order_num': None, 'order_verified': False,
            'create_reply_order_num': None,
            'invitation_confirmed': False,
            'cancel_verified': False,
            'local_test_allowance_replenished_by_cancellation': False,
            'cancellation_budget_before_test': budget,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise TestAlreadyUsed('本次真实测试机会已经登记，禁止重新创建测试订单') from exc
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        self.data = data
        self._owner = True
        return data

    def load_readonly(self):
        self.data = json.loads(self.path.read_text(encoding='utf-8'))
        self._owner = False
        return self.data

    def _save(self):
        temporary = self.path.with_name(self.path.name + '.tmp')
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(self.data, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    def reserve_request(self, endpoint, order_num=None):
        with self._guard:
            if not self._owner or self.data is None:
                raise TestAlreadyUsed('已有测试只能核对结果，不能再次发起写请求')
            if endpoint not in self.ENDPOINTS:
                raise BookingError('单次测试不允许此操作')
            marker = self.path.with_name(self.path.name + '.' + endpoint)
            if self.data['requests'][endpoint] or marker.exists():
                raise TestAlreadyUsed('该测试请求已经登记，禁止重复发送')
            if endpoint == 'creat_book_info' and self.data['phase'] != 'CLAIMED':
                raise TestAlreadyUsed('本次测试不再允许发起预约校验')
            if endpoint == 'creat_order' and self.data['phase'] != 'VALIDATED':
                raise BookingError('没有完成本次预约校验，禁止创建')
            if endpoint == 'cancel_order':
                if (self.data['phase'] != 'ORDER_VERIFIED' or not self.data['order_verified']
                        or not self.data['invitation_confirmed'] or str(order_num) != self.data['order_num']):
                    raise BookingError('禁止取消未经本次测试核实的订单')
            try:
                descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise TestAlreadyUsed('该测试请求已经登记，禁止重复发送') from exc
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                json.dump({'endpoint': endpoint, 'reserved_at': datetime.now(TZ).isoformat()}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            self.data['requests'][endpoint] = 1
            self.data['phase'] = {
                'creat_book_info': 'VALIDATION_REQUESTED',
                'creat_order': 'CREATE_REQUESTED',
                'cancel_order': 'CANCEL_REQUESTED',
            }[endpoint]
            self._save()

    def record_validation_accepted(self):
        with self._guard:
            if not self._owner or self.data['phase'] != 'VALIDATION_REQUESTED':
                raise BookingError('没有本次校验请求，不能登记通过')
            self.data['phase'] = 'VALIDATED'
            self._save()

    def record_response(self, endpoint, status, signature=None):
        with self._guard:
            if not self._owner or endpoint not in self.ENDPOINTS or self.data['requests'][endpoint] != 1:
                raise BookingError('缺少本次请求记录，不能登记回复')
            self.data['last_reply'] = {'endpoint':endpoint, 'status':status,
                                       'signature':signature}
            self._save()

    def record_order(self, order, plan):
        with self._guard:
            if not self._owner or self.data['requests']['creat_order'] != 1:
                raise BookingError('没有本次创建记录，不能绑定订单')
            if self.data['phase'] not in ('CREATE_REPLY_RECEIVED', 'ORDER_VERIFIED'):
                raise BookingError('本次测试状态不允许重新绑定订单')
            expected = self.data['plan']
            if (plan.day, plan.venue, plan.venue_type, plan.start, plan.end) != (
                expected['day'], expected['venue'], expected['venue_type'],
                expected['start'], expected['end']):
                raise BookingError('测试计划发生变化，停止操作')
            if not exact_order(order, plan, site=expected['site']):
                raise BookingError('订单与本次测试不符，禁止取消')
            if order.get('venue_type') != 'tennis':
                raise BookingError('订单项目未经确认为网球，停止操作')
            checked_price(order.get('total_price'), 0)
            friends = order.get('with_friends')
            if type(friends) not in (bool, int) or friends not in (0, 1):
                raise BookingError('订单邀请状态无法确认，停止操作')
            number = str(order['order_num'])
            if self.data['order_num'] is not None and self.data['order_num'] != number:
                raise BookingError('本次测试订单号已经锁定，禁止改绑')
            if self.data['create_reply_order_num'] is None or str(self.data['create_reply_order_num']) != number:
                raise BookingError('订单号未经本次创建回复确认，禁止取消')
            if number in self.data['baseline_order_numbers']:
                raise BookingError('订单在测试前已经存在，禁止取消')
            self.data.update(order_num=number, order_verified=True, phase='ORDER_VERIFIED',
                             invitation_confirmed=bool(friends))
            self._save()

    def record_create_reply(self, number):
        with self._guard:
            if not self._owner or self.data['phase'] != 'CREATE_REQUESTED' or self.data['create_reply_order_num'] is not None:
                raise BookingError('没有本次创建请求，不能绑定回复')
            if type(number) not in (str, int) or not str(number):
                raise BookingError('创建回复没有明确订单号')
            self.data['create_reply_order_num'] = number
            self.data['phase'] = 'CREATE_REPLY_RECEIVED'
            self._save()

    def record_cancel_verified(self):
        with self._guard:
            if not self._owner or self.data['phase'] != 'CANCEL_REQUESTED' or not self.data['order_verified'] or self.data['requests']['cancel_order'] != 1:
                raise BookingError('缺少本次取消证据')
            self.data.update(cancel_verified=True, phase='CANCELLED_VERIFIED')
            self._save()

    def record_failure(self, reason, request_timings=()):
        with self._guard:
            if not self._owner:
                raise TestAlreadyUsed('只读记录不能改写测试状态')
            self.data.update(phase='STOPPED', reason=reason)
            allowed = {'endpoint', 'status', 'error_type', 'attempt', 'elapsed_ms'}
            self.data['request_diagnostics'] = [{key:value for key,value in row.items() if key in allowed}
                                                for row in list(request_timings)[-6:]]
            self._save()
