"""任务目录：暂存、工作区、归属标记与安全清理。

四层目录各司其职，混用会导致误删或把别人下的东西当自己的：

    TMP_ROOT        迅雷的「下载到」根，里面可能有别人的任务
    <TMP_ROOT>/<键>  本次任务唯一的下载目录（同时也是迅雷任务名）
    .x-drive-owner   本程序放置的归属标记；没有它的目录不归我们处理
    state/          整理后的工作区（改名、挑主体都在这里做，不动下载原件）

两条纪律，缺一不可：
  · claim() 绝不接管「已经存在但没有标记」的目录 —— 那是别人的数据；
  · drop() 只认带标记且键相同的目录，其余一律跳过。

迅雷会因重名把目录改成 `<键>-1`，所以清理必须用迅雷上报的真实路径，
再用 owned_path() 确认它和本任务一一对应，不能只看名字前缀。
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger("x-drive.workspace")

OWNER_MARK = ".x-drive-owner"

def key_of(raw: str) -> str:
    """任务键归一：只留字母数字。空键给一个稳定的兜底名。"""
    return "".join(ch for ch in (raw or "") if ch.isalnum()) or "job"

def staging(tmp_root: str, key: str) -> Path:
    """下载目录 `<TMP_ROOT>/<键>`。"""
    return Path(tmp_root) / key_of(key)

def workspace(root: str, key: str) -> Path:
    """整理工作区 `state/<键>`：改名、挑主体、上传都读这里，不碰下载原件。"""
    return Path(root) / "state" / key_of(key)

def _mark(directory: Path) -> dict:
    """读归属标记。老格式只有 task_id、时间、来源三行，按 owner 处理。"""
    try:
        lines = (directory / OWNER_MARK).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    fields = {"owner": (lines[0].strip() if lines else "")}
    for line in lines[1:]:
        name, sep, value = line.partition("=")
        if sep:
            fields[name.strip()] = value.strip()
    return fields

def _mark_owner(directory: Path) -> str:
    return _mark(directory).get("owner", "")

def _write_mark(directory: Path, key: str, evidence: dict) -> None:
    lines = [key_of(key), f"at={time.strftime('%Y-%m-%dT%H:%M:%S')}"]
    for name, value in evidence.items():
        if value not in (None, ""):
            lines.append(f"{name}={str(value)[:200]}")
    (directory / OWNER_MARK).write_text("\n".join(lines), encoding="utf-8")

def claim(directory: Path, key: str, source: str = "", task_id: str = "",
          infohash: str = "") -> None:
    """本程序新建目录并打归属标记。

    已经存在但没有标记的目录一律不动 —— 别人手工建的、旧版本留下的都有可能。
    只有「本次新建」或「标记就是本任务」才继续。
    """
    key = key_of(key)
    if (directory / OWNER_MARK).is_file():
        owner = _mark_owner(directory)
        if owner and owner != key:
            raise RuntimeError(f"{directory} 属于另一个任务（{owner}），拒绝覆盖")
        return
    if directory.exists():
        raise RuntimeError(
            f"{directory} 已存在但没有归属标记；不接管既有数据，"
            "请换一个任务键或先自行处理该目录"
        )
    directory.mkdir(parents=True, exist_ok=False)
    _write_mark(directory, key, {"source": source, "task": task_id, "btih": infohash})

def adopt(directory: Path, key: str, source: str = "", task_id: str = "",
          infohash: str = "") -> None:
    """给**本任务投递后**由迅雷新建的重名目录补标记。

    与 claim() 的分工：目录不是我们建的（迅雷见重名自动改成 `<键>-N`），
    但里面是本次任务刚下的文件。调用方必须已确认：
      · 路径形态属于本任务（shape_of）；
      · 该任务不是复用平台里已有任务的（reused=False）；
      · 任务 id 能对上。
    否则就是把别人的下载标成自己的 —— 宁可失败也不接管。
    """
    key = key_of(key)
    if (directory / OWNER_MARK).is_file():
        owner = _mark_owner(directory)
        if owner and owner != key:
            raise RuntimeError(f"{directory} 属于另一个任务（{owner}），拒绝覆盖")
    directory.mkdir(parents=True, exist_ok=True)
    _write_mark(directory, key, {"source": source, "task": task_id, "btih": infohash})

def owned(directory: Path, key: str) -> bool:
    """这个目录是不是本次任务建的。没有标记一律不认。"""
    return bool(key) and _mark_owner(directory) == key_of(key)

def evidence_of(directory: Path) -> dict:
    """标记里的归属证据（task / btih / source），供对账与排查。"""
    return _mark(directory)

def shape_of(path: Path, dest: Path) -> bool:
    """路径形态是否属于本任务（只看形状，不看标记）。

    合法形态只有两种：
      · path 就是 dest；
      · path 与 dest 同父目录且名为 `<dest名>-<数字>`（迅雷重名时自动改名）。
    逐字符比对前缀：旧实现用 path.name[len(dest.name):] 切后缀，短 dest 会把
    `OTHER99-1` 这类不相干目录也算成自己，删起来就是别人的数据。
    """
    if path == dest:
        return True
    if dest in path.parents or path.parent != dest.parent:
        return False
    head, tail = path.name[:len(dest.name)], path.name[len(dest.name):]
    return head == dest.name and tail.startswith("-") and tail[1:].isdigit()

def owned_path(path: Path, dest: Path, key: str) -> bool:
    """迅雷已落盘的目录是否属于本任务：形态对，**且**该目录带本任务标记。

    下载器确认落盘后会用 adopt() 补标记；没标记的目录不认 —— 宁可留在盘上，
    也不能把自己的清理动作指向别人的数据。
    """
    return shape_of(path, dest) and owned(path, key)

def drop(directory: Path, key: str, why: str = "") -> bool:
    """删除本任务的目录。不属于本任务就什么都不做，返回 False。"""
    if not directory.exists():
        return False
    if not owned(directory, key):
        log.warning("跳过清理 %s：没有归属标记（%s）", directory, why or "未说明原因")
        return False
    shutil.rmtree(directory, ignore_errors=True)
    log.info("清理 %s（%s）", directory, why or "任务结束")
    return True

def drop_workspace(directory: Path, key: str, why: str = "") -> bool:
    """工作区没有迅雷产物，删之前只校验路径形态，不要求下载目录的标记。"""
    if not directory.exists():
        return False
    if directory.name != key_of(key):
        log.warning("跳过清理 %s：目录名与任务键不符", directory)
        return False
    shutil.rmtree(directory, ignore_errors=True)
    log.info("清理工作区 %s（%s）", directory, why or "任务结束")
    return True
