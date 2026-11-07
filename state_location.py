"""Bind all copies for this Windows user to one durable state directory."""
import json
import os
from pathlib import Path

def resolve_state(root, local_data=None):
    root=Path(root).resolve()
    local_data=Path(local_data or os.environ['LOCALAPPDATA'])
    home=local_data/'HDUVenueAssistant'
    binding=home/'state-location.json'
    legacy=root/'.private'
    home.mkdir(parents=True,exist_ok=True)
    if not binding.exists():
        chosen=legacy if legacy.is_dir() else home/'state'
        chosen.mkdir(parents=True,exist_ok=True)
        raw=json.dumps({'schema':1,'directory':str(chosen.resolve())},ensure_ascii=False).encode('utf-8')
        try:
            with binding.open('xb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
        except FileExistsError:pass
    try:
        info=json.loads(binding.read_text(encoding='utf-8'))
        chosen=Path(info['directory'])
        if info.get('schema')!=1 or not chosen.is_absolute() or not chosen.is_dir():raise ValueError()
    except (OSError,ValueError,KeyError,TypeError):
        raise RuntimeError('原预约状态目录不可访问。请恢复原目录或联系维护者核对，不要清除防重复记录。') from None
    # Preserve guards from an earlier source checkout without deleting or changing originals.
    # Existing guards always win, including a different/unknown payload for that day.
    if legacy.is_dir() and legacy.resolve()!=chosen.resolve():
        for pattern in ('validation-*.json','submitted-*.json'):
            for source in legacy.glob(pattern):
                if not source.is_file():continue
                try:
                    with (chosen/source.name).open('xb') as stream:
                        stream.write(source.read_bytes());stream.flush();os.fsync(stream.fileno())
                except FileExistsError:pass
    return chosen.resolve()
