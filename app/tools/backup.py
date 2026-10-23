# -*- coding: utf-8 -*-
"""数据备份：把 `data\\` 整个打包成一个 zip，只保留最近几份。

两个要点：

1. **数据库用 SQLite 自己的 backup API 导一份快照再打包**，不是直接拷文件 ——
   服务正在写的时候直接拷 `.db` 有可能拿到不一致的半截文件。
2. 备份里包含 `data\\` 下的**全部**东西：数据库、发票归档、邮箱待归档、样式图片、邮箱配置。
   恢复是把 zip 解压到 **`data\\` 目录里面**（zip 内是 `data` 的内部相对路径，
   不带 `data/` 这一层）—— 解压到项目根目录会变成 `项目根\\procurement.db`，等于没恢复。
"""
import os
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from app import config

BACKUP_DIR_NAME = '备份'
PREFIX = 'data_'
DEFAULT_KEEP = 10


def backup_dir(dest=None) -> Path:
    return Path(dest) if dest else (config.ROOT / BACKUP_DIR_NAME)


def list_backups(dest=None) -> List[dict]:
    d = backup_dir(dest)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob('%s*.zip' % PREFIX)):
        st = p.stat()
        out.append({'name': p.name, 'path': str(p),
                    'size': st.st_size, 'mtime': st.st_mtime})
    return out


def prune(dest=None, keep: int = DEFAULT_KEEP) -> List[str]:
    """只留最近 keep 份，返回被删掉的文件名。"""
    keep = max(1, int(keep or DEFAULT_KEEP))
    files = list_backups(dest)
    removed = []
    for item in files[:-keep]:
        try:
            Path(item['path']).unlink()
            removed.append(item['name'])
        except OSError:
            pass
    return removed


def _db_snapshot(tmp_dir: Path) -> Optional[Path]:
    """用 sqlite 的 backup API 导一份一致的数据库快照。"""
    if not config.DB_PATH.exists():
        return None
    tmp = tmp_dir / 'snapshot.db'
    src = sqlite3.connect(str(config.DB_PATH))
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return tmp


def backup(dest=None, keep: int = DEFAULT_KEEP, now: Optional[datetime] = None) -> dict:
    """打包一次。返回统计信息。"""
    src = Path(config.DATA_DIR)
    if not src.exists():
        raise FileNotFoundError('还没有 data 目录，没什么可备份的')

    out_dir = backup_dir(dest)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime('%Y%m%d_%H%M%S')
    target = out_dir / ('%s%s.zip' % (PREFIX, stamp))

    files = 0
    total = 0
    with tempfile.TemporaryDirectory() as td:
        snapshot = _db_snapshot(Path(td))
        with zipfile.ZipFile(str(target), 'w', zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(src.rglob('*')):
                if not path.is_file():
                    continue
                # 别把备份装进它自己：如果备份目录恰好在 data 里（不该这么配），
                # 每备份一次都会把上一份也打进去，体积会指数膨胀。
                if out_dir == src or out_dir in path.parents:
                    continue
                rel = path.relative_to(src).as_posix()
                if path == Path(config.DB_PATH) and snapshot is not None:
                    zf.write(str(snapshot), rel)      # 写一致快照，不写原文件
                else:
                    zf.write(str(path), rel)
                files += 1
                total += path.stat().st_size

    removed = prune(dest, keep)
    return {'path': str(target), 'name': target.name, 'files': files,
            'bytes': total, 'size': target.stat().st_size, 'removed': removed}


def human_size(n) -> str:
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return '%.1f %s' % (n, unit)
        n /= 1024


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if '--list' in argv:
        rows = list_backups()
        if not rows:
            print('还没有备份。')
            return 0
        print('已有 %d 份备份（新→旧）：' % len(rows))
        for item in reversed(rows):
            when = datetime.fromtimestamp(item['mtime']).strftime('%Y-%m-%d %H:%M')
            print('  %-24s %8s  %s' % (item['name'], human_size(item['size']), when))
        return 0

    keep = DEFAULT_KEEP
    if '--keep' in argv:
        i = argv.index('--keep')
        if i + 1 < len(argv) and argv[i + 1].isdigit():
            keep = int(argv[i + 1])

    try:
        info = backup(keep=keep)
    except Exception as exc:
        print('备份失败：%s' % exc)
        return 1

    print('已备份 %d 个文件（原始 %s）' % (info['files'], human_size(info['bytes'])))
    print('  → %s（%s）' % (info['path'], human_size(info['size'])))
    if info['removed']:
        print('  顺手清掉了 %d 份旧备份：%s'
              % (len(info['removed']), '、'.join(info['removed'])))
    print('  保留最近 %d 份。' % keep)
    print()
    print('恢复办法：把 zip 解压到 data 目录里面（zip 内不带 data/ 这一层），'
          '覆盖同名文件后重启服务。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
