"""One authorized tennis order, followed only by cancellation of that exact order."""
import json
import time
from datetime import datetime

from booking import (BookingError, NetworkError, Trial, TZ, Uncertain, exact_order, indices,
                     order_is_cancelled, overlaps, payload_for)


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)


class GuardedTestApi:
    READS = frozenset({'post_server_time', 'post_order_info', 'post_venue_info', 'post_site_situation'})

    def __init__(self, api, journal, expected_payload):
        self.api = api
        self.journal = journal
        self.expected_payload = _canonical(expected_payload)

    @property
    def session(self):
        return self.api.session

    @property
    def timings(self):
        return getattr(self.api, 'timings', [])

    def orders(self, invited=False, timeout=5):
        reply = self.call('post_order_info', {'openid': self.session['openid'], 'is_invite': invited}, timeout=timeout)
        if reply.status != 200 or not isinstance(reply.data.get('order_info'), list):
            raise BookingError('无法核对本次订单')
        return reply.data['order_info']

    def call(self, endpoint, payload=None, timeout=5):
        if endpoint in self.READS:
            return self.api.call(endpoint, payload, timeout=timeout)
        if endpoint in ('creat_book_info', 'creat_order'):
            if _canonical(payload) != self.expected_payload:
                raise BookingError('测试请求与已锁定的免费网球计划不符')
            self.journal.reserve_request(endpoint)
        elif endpoint == 'cancel_order':
            if not isinstance(payload, dict) or set(payload) != {'order_num'}:
                raise BookingError('取消请求格式不符')
            self.journal.reserve_request(endpoint, payload['order_num'])
        else:
            raise BookingError('单次测试禁止调用未核定的接口')
        reply = self.api.call(endpoint, payload, timeout=timeout)
        signature = None
        if reply.status >= 400:
            text = json.dumps(reply.data, ensure_ascii=False)
            signature = ('uninitialized_self_book' if 'self_book' in text and 'before assignment' in text
                         else 'unclassified_http_error')
        self.journal.record_response(endpoint, reply.status, signature)
        if endpoint == 'creat_book_info' and reply.status == 200:
            conflicts, available = reply.data.get('conflict_times'), reply.data.get('available_times')
            wanted = json.loads(self.expected_payload)['orderData']['time_list']
            if (isinstance(conflicts, list) and isinstance(available, list) and not conflicts
                    and all(type(value) is int for value in available)
                    and (not available or set(wanted).issubset(available))):
                self.journal.record_validation_accepted()
        if endpoint == 'creat_order' and 200 <= reply.status < 300:
            data = reply.data.get('data')
            number = data.get('order_num') if isinstance(data, dict) else None
            if type(number) in (str, int) and str(number):
                self.journal.record_create_reply(number)
        return reply


class SingleTestFlow:
    def __init__(self, api, journal, plan, meta, option, cancellation_budget,
                 clock=None, sleep=time.sleep, monotonic=time.monotonic,
                 publish=None, invitation_timeout=180):
        self.api, self.journal, self.plan = api, journal, plan
        self.meta, self.option = meta, option
        self.budget, self.clock, self.sleep = cancellation_budget, clock, sleep
        self.monotonic, self.publish = monotonic, publish or (lambda **event: None)
        if type(invitation_timeout) is not int or not 1 <= invitation_timeout <= 180:
            raise BookingError('等待邀请期限必须在1至180秒内')
        self.invitation_timeout = invitation_timeout

    def ensure_cancellation_window(self, extra_seconds=0):
        try:
            start = datetime.strptime(self.plan.day + ' ' + self.plan.start,
                                      '%Y-%m-%d %H:%M').replace(tzinfo=TZ).timestamp()
            now = self.clock.now() if self.clock is not None else time.time()
        except (TypeError, ValueError, OverflowError) as exc:
            raise BookingError('无法确认取消截止时间，禁止真实测试') from exc
        if not start - now > 3600 + extra_seconds:
            raise BookingError('取消须提前超过一小时，当前时间不符合')

    def prepare_creation(self, plan, site):
        self.budget.ensure_one_safe_cancellation(required_remaining=self.invitation_timeout + 30)
        self.ensure_cancellation_window(self.invitation_timeout + 30)

    def wait_for_manual_invitation(self, guarded):
        budget_remaining = 300 - (datetime.now(TZ) - self.budget.observed_at).total_seconds()
        deadline = self.monotonic() + min(self.invitation_timeout, max(0, budget_remaining - 15))
        announced = False
        while True:
            self.ensure_cancellation_window()
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise Uncertain('等待邀请已超时，未发送取消；本次订单已创建，请立即在钉钉处理，禁止再建单')
            try:
                current = [item for item in guarded.orders(False, timeout=min(5, remaining / 2))
                           if str(item.get('order_num')) == self.journal.data['order_num']]
            except NetworkError:
                current = None
            if self.monotonic() > deadline:
                raise Uncertain('等待邀请查询已超过期限，未发送取消；请立即在钉钉核对本次订单')
            if current is not None:
                if len(current) != 1 or not exact_order(current[0], self.plan, site=self.option.site):
                    raise Uncertain('本次订单状态已变化，未执行脚本取消；禁止再建订单，请核对')
                self.journal.record_order(current[0], self.plan)
                if self.journal.data['invitation_confirmed']:
                    self.publish(state='INVITATION_CONFIRMED', message='系统已确认邀请同伴，即将取消本次测试订单')
                    return current[0]
            if not announced:
                self.publish(state='WAITING_FOR_INVITATION',
                             message='本次订单已核实，请现在到钉钉手动邀请一名同伴，脚本最多等待3分钟',
                             day=self.plan.day, venue=self.plan.venue,
                             start=self.plan.start, end=self.plan.end, site_id=self.option.site)
                announced = True
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise Uncertain('等待邀请已超时，未发送取消；本次订单已创建，请立即在钉钉处理，禁止再建单')
            self.sleep(min(5, remaining))

    def run(self):
        plan, option = self.plan, self.option
        if plan.venue_type != 'tennis' or plan.allow_one_hour or option.plan != plan or option.total != 0:
            raise BookingError('本次测试只允许一个预先选定的免费网球时段，不自动换时段')
        expected = payload_for(self.api.session, self.meta, plan, option.site, option.total)
        wanted = indices(self.meta, plan)
        marks = [datetime.strptime(value, '%H:%M') for value in
                 self.meta['time_list'][wanted[0]:wanted[-1]+2]]
        if len(wanted) != 2 or [(right-left).total_seconds() for left, right in zip(marks, marks[1:])] != [3600, 3600]:
            raise BookingError('本次测试须将连续两个一小时时段合成一单')
        self.prepare_creation(plan, option.site)
        mine, invited = self.api.orders(False), self.api.orders(True)
        if any(overlaps(order, plan) for order in mine + invited):
            raise BookingError('目标时段已有预约或受邀记录，不开始真实测试')
        self.journal.claim(plan, option.site, [order['order_num'] for order in mine], self.budget)
        guarded = GuardedTestApi(self.api, self.journal, expected)
        trial = Trial(guarded, plan, clock=self.clock, shuffle=lambda values: None,
                      before_create=self.prepare_creation)
        try:
            order = trial.attempt(self.meta, [option], prechecked=False)
            self.journal.record_order(order, plan)
            order = self.wait_for_manual_invitation(guarded)
            self.budget.ensure_one_safe_cancellation()
            self.ensure_cancellation_window()
            cancel_reply = None
            try:
                cancel_reply = guarded.call('cancel_order', {'order_num': order['order_num']})
            except BookingError:
                # Never re-send cancellation merely because its reply was lost.
                pass
            absent = 0
            for delay in (0, .5, 1, 2):
                if delay:
                    self.sleep(delay)
                try:
                    matches = [item for item in guarded.orders(False)
                               if str(item.get('order_num')) == self.journal.data['order_num']]
                    inactive = not matches or (len(matches) == 1 and order_is_cancelled(matches[0]))
                except BookingError:
                    absent = 0
                    continue
                absent = absent + 1 if inactive else 0
                if absent >= 2:
                    self.journal.record_cancel_verified()
                    return {'state': 'CANCELLED_VERIFIED', 'created_orders': 1,
                            'cancel_requests': 1,
                            'cancel_http_status': cancel_reply.status if cancel_reply else None,
                            'local_test_allowance_replenished': False}
            raise Uncertain('已发出本次取消请求，但尚未核实订单失效；禁止再建订单')
        except (BookingError, OSError, ValueError, TypeError) as exc:
            try:
                self.journal.record_failure(type(exc).__name__, guarded.timings)
            except OSError:
                pass
            raise
