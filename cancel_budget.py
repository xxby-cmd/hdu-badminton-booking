"""Evidence needed before spending the one permitted real cancellation."""
from dataclasses import dataclass
from datetime import datetime

from booking import BookingError, TZ


@dataclass(frozen=True)
class CancellationBudget:
    used_count: int
    restriction_at: int
    period: str
    source: str
    observed_at: datetime
    rule_confirmed: bool = False
    count_confirmed: bool = False

    def ensure_one_safe_cancellation(self, now=None, required_remaining=0):
        now = now or datetime.now(TZ)
        if not self.rule_confirmed or not self.count_confirmed or not self.period or not self.source:
            raise BookingError('取消规则或当前已用次数尚未确认，禁止开始真实测试')
        if (type(self.used_count) is not int or self.used_count < 0
                or type(self.restriction_at) is not int or self.restriction_at < 1):
            raise BookingError('取消次数数据无效，禁止开始真实测试')
        if type(required_remaining) not in (int, float) or not 0 <= required_remaining <= 300:
            raise BookingError('取消资料所需有效时间无效')
        if self.observed_at.tzinfo is None or not 0 <= (now - self.observed_at).total_seconds() <= 300-required_remaining:
            raise BookingError('取消次数资料已过期，必须重新核对')
        # The user's ceiling is four; do not reach it even if another source is looser.
        if self.used_count + 1 >= min(4, self.restriction_at):
            raise BookingError('这次取消可能达到限制次数，禁止开始真实测试')
        return {
            'used_count': self.used_count,
            'restriction_at': min(4, self.restriction_at),
            'period': self.period, 'source': self.source,
            'observed_at': self.observed_at.isoformat(),
        }
