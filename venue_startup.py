"""Wait for a usable venue login without touching booking endpoints."""
import os
import time

from booking import BookingError, Stopped
from session import SessionAccountChanged, SessionError

ENTRANCE = ('dingtalk://dingtalkclient/page/link?'
            'url=https%3A%2F%2Fsportmeta.hdu.edu.cn%2Fbook%2Fdingtalk%2F%23%2F'
            '&popup_wnd=true&width=1080&height=860&title=%E5%9C%BA%E9%A6%86%E9%80%9F%E7%BA%A6')


def wait_for_session(release, stop, publish, load, *, now=time.time, open_entrance=None):
    open_entrance = open_entrance or (lambda: os.startfile(ENTRANCE))
    near_release = release - 20 * 60
    opened = [0, 0]
    next_open = float('-inf')
    last_message = None
    while True:
        if stop.is_set():
            raise Stopped('脚本已停止')
        current = now()
        if current >= release:
            raise BookingError('放场前仍未取得有效场馆登录，本次未发起预约')
        try:
            return load(release)
        except SessionAccountChanged as exc:
            raise BookingError(str(exc)) from None
        except (SessionError, OSError):
            pass
        phase = int(current >= near_release)
        if opened[phase] < (2, 3)[phase] and current >= next_open:
            opened[phase] += 1
            next_open = current + 45
            try:
                open_entrance()
                message = '已打开钉钉场馆入口，请在电脑钉钉登录。脚本会自动检查并继续。'
            except OSError:
                message = '暂时无法打开钉钉场馆入口，脚本会重试；也可以手动打开场馆页。'
        else:
            message = '仍在等待有效场馆登录。请保持钉钉登录，场馆页如仍显示旧登录，请关闭后重新打开。'
        if message != last_message:
            publish(state='WAITING_LOGIN', message=message)
            last_message = message
        stop.wait(max(0, min(10, release - now())))
