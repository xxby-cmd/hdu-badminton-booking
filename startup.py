"""Dependency-free launcher shared by the source tree and the portable bundle."""
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent

def setup():
    if sys.platform!='win32':raise RuntimeError('本版本适用于 Windows 10/11 64 位电脑。')
    if sys.version_info<(3,11):raise RuntimeError('需要 Python 3.11 以上；请使用完整便携包中的启动入口。')
    import ctypes, struct, ssl, tkinter
    if struct.calcsize('P')!=8:raise RuntimeError('请使用 64 位版本。')
    from state_location import resolve_state
    # Source, portable bundle and its developer copy all share one user's guard state.
    os.environ['HDU_BOOKING_STATE_DIR']=str(resolve_state(ROOT))
    private=Path(os.environ.get('HDU_BOOKING_STATE_DIR',str(ROOT/'.private')))
    private.mkdir(parents=True,exist_ok=True)
    import tempfile
    with tempfile.TemporaryFile(dir=private) as stream:stream.write(b'write-check')
    if not ctypes.WinDLL('crypt32'):raise RuntimeError('Windows 账户加密组件不可用。')
    root=tkinter.Tk();root.withdraw();root.update_idletasks();root.destroy()
    return {'python':sys.version.split()[0],'tk':tkinter.TkVersion,'ssl':ssl.OPENSSL_VERSION,
            'ready':True,'private_is_writable':True}

def main():
    try:
        result=setup()
        if '--self-check' in sys.argv:
            print(json.dumps(result,ensure_ascii=True));return 0
        from control_panel import main as panel
        return panel(sys.argv[1:])
    except Exception as exc:
        # Never print session, token, account, or raw traceback in the launch window.
        if isinstance(exc,RuntimeError):message=str(exc)
        else:message='运行环境检查失败。请解压完整文件夹，使用 Windows 10/11 64 位电脑，并保留所有文件。'
        print(message)
        return 1

if __name__=='__main__':raise SystemExit(main())
