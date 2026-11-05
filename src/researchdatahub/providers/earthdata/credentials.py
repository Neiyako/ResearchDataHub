"""Earthdata Login（EDL）凭证探测：**只检查存在性，绝不读取/打印值**。

真实支持的两种路径（NASA 官方 Earthdata Login 文档 + 2026-11-05 本机实测）
--------------------------------------------------------------------------
1. ``EARTHDATA_TOKEN`` 环境变量 —— EDL 生成的 Bearer token（``Authorization: Bearer <token>``），
   用于受保护数据下载与部分受保护 service。
2. ``~/.netrc`` 中的 ``machine urs.earthdata.nasa.gov`` 记录 —— ``urllib``/``requests`` 会在
   访问 EDL 域时自动带上。**本模块只解析主机名条目是否存在**，不解析也不保存 login/password。

本机 2026-11-05 实测状态
------------------------
``EARTHDATA_TOKEN`` 未设置，``~/.netrc`` 不存在。因此"带凭证下载受保护 granule"这一条
在能力矩阵里是 **unverified**，不是 verified。

安全约束
--------
* :class:`EarthdataCredentialState` **不携带任何凭证值**；只记录 "存在/不存在"。
* 从 netrc 只取 ``machine`` 名字做匹配；``login``/``password`` 一旦出现也不进入本对象，
  且异常信息里不带原文。
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "EARTHDATA_LOGIN_HOST",
    "TOKEN_ENV_VAR",
    "EarthdataCredentialState",
    "probe_credentials",
]

TOKEN_ENV_VAR: Final[str] = "EARTHDATA_TOKEN"
"""EDL Bearer token 的环境变量名（官方推荐用于脚本化访问）。"""

EARTHDATA_LOGIN_HOST: Final[str] = "urs.earthdata.nasa.gov"
"""Earthdata Login 主机；``~/.netrc`` 的 ``machine`` 条目按此匹配。"""


@dataclass(frozen=True)
class EarthdataCredentialState:
    """凭证存在性快照（**不含任何凭证内容**，可安全写入日志与能力报告）。"""

    token_present: bool
    """``EARTHDATA_TOKEN`` 是否设置了非空值。"""

    netrc_present: bool
    """``~/.netrc`` 文件是否存在且权限安全（其它用户不可读）。"""

    netrc_has_edl_machine: bool
    """``~/.netrc`` 中是否存在 ``machine urs.earthdata.nasa.gov`` 条目。"""

    netrc_path: str
    """检查过的 netrc 路径（仅为便于排障）。"""

    def download_credential_available(self) -> bool:
        """是否**可能**具备下载受保护数据的凭证（token 或 netrc 任一存在）。

        注意：这里只能判断"可能"。凭证是否真的有效（是否过期、是否有该数据集的授权）
        必须由一次真实请求验证，**不得**据此声称下载已验证。
        """
        return self.token_present or self.netrc_has_edl_machine

    def missing(self) -> tuple[str, ...]:
        """缺失的凭证来源（供 ``unverified`` 说明使用）。"""
        missing: list[str] = []
        if not self.token_present:
            missing.append(f"env {TOKEN_ENV_VAR} is not set")
        if not self.netrc_has_edl_machine:
            missing.append(f"~/.netrc has no 'machine {EARTHDATA_LOGIN_HOST}' entry")
        return tuple(missing)

    def to_dict(self) -> dict[str, bool | str]:
        """可 JSON 序列化的快照（**永远不含凭证值**）。"""
        return {
            "token_present": self.token_present,
            "netrc_present": self.netrc_present,
            "netrc_has_edl_machine": self.netrc_has_edl_machine,
            "netrc_path": self.netrc_path,
        }


def _netrc_has_edl_machine(path: Path) -> tuple[bool, bool]:
    """返回 ``(文件存在且权限安全, 含 EDL machine 条目)``；**不读取 login/password 值**。

    安全策略：权限过宽（group/other 可读）的 netrc 视为不可信，返回 ``(False, False)``
    并提示用户修正——这与 ``curl``/``netrc`` 的常规安全实践一致。
    """
    try:
        info = path.stat()
    except OSError:
        return False, False
    if not path.is_file():
        return False, False
    if stat.S_IMODE(info.st_mode) & (stat.S_IRWXG | stat.S_IRWXO):
        return False, False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, False
    # 只做逐 token 的主机名匹配；匹配到即返回，不保留其它字段。
    tokens = text.split()
    found = False
    for index, token in enumerate(tokens):
        if token == "machine" and index + 1 < len(tokens):
            host = tokens[index + 1]
            if host == EARTHDATA_LOGIN_HOST:
                found = True
                break
    return True, found


def probe_credentials(
    *,
    environ: Mapping[str, str] | None = None,
    netrc_path: Path | None = None,
) -> EarthdataCredentialState:
    """探测 EDL 凭证存在性（纯本地文件/环境检查，**不发网络请求**）。

    Parameters
    ----------
    environ:
        环境变量映射；默认 ``os.environ``（测试可注入）。
    netrc_path:
        netrc 路径；默认 ``~/.netrc``（测试可注入临时文件）。
    """
    env = os.environ if environ is None else environ
    token = env.get(TOKEN_ENV_VAR, "")
    path = netrc_path if netrc_path is not None else Path.home() / ".netrc"
    netrc_present, has_edl = _netrc_has_edl_machine(path)
    return EarthdataCredentialState(
        token_present=bool(token.strip()),
        netrc_present=netrc_present,
        netrc_has_edl_machine=has_edl,
        netrc_path=str(path),
    )
