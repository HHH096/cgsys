# -*- coding: utf-8 -*-
"""把项目打成一个 zip，好传到云服务器上。

**必须排除 `.venv`** —— 那是 Windows 的虚拟环境，Linux 上一点用都没有，
而且文件又多又大（传上去要好半天）。服务器上会自己建 Linux 版的。

`data/` 是要带上的：里面有名单、已经填的采购、发票。迁过去数据才不会丢。
"""
import zipfile
from pathlib import Path
from typing import Tuple

from app import config

# 这些目录整个跳过
EXCLUDE_DIRS = {
    '.venv', '__pycache__', '.reasonix',
    '导出', '备份', 'cloudflared',
}
# 这些后缀跳过（日志没用，字节码是平台相关的）
EXCLUDE_SUFFIX = {'.pyc', '.pyo', '.log'}
OUT_NAME = '_上传给服务器.zip'


def pack(root=None, out_dir=None) -> Tuple[Path, int, int]:
    """返回 (zip 路径, 打包了几个文件, 原始总字节)。"""
    root = Path(root or config.ROOT)
    out = Path(out_dir or root) / OUT_NAME
    files = 0
    total = 0

    with zipfile.ZipFile(str(out), 'w', zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob('*')):
            if not path.is_file() or path == out:
                continue
            rel = path.relative_to(root)
            if set(rel.parts) & EXCLUDE_DIRS:
                continue
            if path.suffix.lower() in EXCLUDE_SUFFIX:
                continue
            # zip 里套一层顶层目录，解压出来是个整文件夹，不会散一地
            zf.write(str(path), str(Path(root.name) / rel))
            files += 1
            total += path.stat().st_size

    return out, files, total


def human(n) -> str:
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return '%.1f %s' % (n, unit)
        n /= 1024


def main() -> int:
    out, files, total = pack()
    size = out.stat().st_size
    print('打好了：%s' % out)
    print('  %d 个文件，原始 %s，压缩后 %s' % (files, human(total), human(size)))
    print()
    print('下一步：')
    print('  1. 进阿里云 ECS 的「Workbench 远程连接」（网页终端）')
    print('  2. 用界面上的「上传文件」把这个 zip 传上去')
    print('  3. 在终端里照《迁移到云服务器.md》第 2 节往下做')
    print()
    print('（.venv 没有打进去 —— 那是 Windows 的环境，服务器上会自己建。）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
