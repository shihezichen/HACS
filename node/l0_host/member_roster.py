#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L0 平台层：**成员名册档案**（阶段5 第三刀 · 刀27 新增）。

一句话职责
----------
回答「**这个协作域有哪些成员、各自是什么角色、它的口令真值落在哪台机的哪个文件**」——
仅此三样。它是名册的**唯一真源**，不是配置的副本。

为什么要把名册从中枢配置里提出来
--------------------------------
刀25（阶段5 第一刀）把名册真源定义为「两台 v2 中枢 `nats.conf` 里 `authorization.users`
的规范形」，并写明「名册提升为独立档案件是阶段5 后续刀的事」。那句话的落点就是本件。

在这之前，「谁是成员」这件事**只存在于中枢机的运行期配置里** —— 要加人就得登上中枢手改
`nats.conf`，加完之后仓里没有任何痕迹，对账器也只能拿配置去对配置（自证）。名册一旦独立
成档案，链条才转得过来：

    档案（谁是成员）  +  探针（口令真值在哪）   ⇒   中枢配置的用户区

阶段5 的收口刀要让**生成器按这个式子写配置**、让**对账器把期望侧换成这个式子的左边**。
刀27 本身**不切生成器、不切对账器**，只做一件事：用 `tools/roster_derive_conf.py`
**机械证明这个式子现在就成立**（推导出的用户区与两台中枢现网的用户区归一化后逐字节相等）。
证不成立，后面两刀就不该动手；证成立了，切换才是「已知等价的替换」而不是「换个写法试试」。

条目形态（**契约，冻结**）
--------------------------
每条 = `(member, role, cred_node, cred_kind)` 四元组：

  · ``member``     成员名。**顺序即规范序（字典序）** —— 与中枢 conf 里 users 的排列同序，
                   这是「推导侧 vs 现网侧逐字节对照」能成立的前提，不许打乱。
  · ``role``       角色名。本刀只有 ``"member"`` 一个值，为将来多角色预留。
  · ``cred_node``  该成员的口令真值落在**哪台机**（节点名，不是地址 → 可入仓）。
  · ``cred_kind``  落在该机的**哪一类位置**：
                     ``"runtime"`` → 运行目录下的 ``nats-<member>.pass``
                     ``"conf"``    → NATS 配置目录下的 ``<member>.pass``

三条铁律
--------
  · **不存口令**：档案只说「口令真值在哪台机的哪个文件」，一个字节的口令都不进本件。
    真值由探针（`tools/roster_derive_conf.py` 的采集面）到那台机上去读。
  · **不写路径、不写地址**：`cred_kind` 是**位置的种类**不是路径。由 `cred_kind` 算出绝对路径
    是 L0 节点档案 `node_profile`（`runtime_dir()` / `nats_conf_dir_posix()`）与部署表的事。
    本件里没有任何 ``/`` 开头的路径常量、没有任何 IP 字面量。
  · **不存权限串**：角色模板的唯一出处是 L1 `gen_nats_conf.perm_list(user)`。本件只记角色**名**。
    （所以本件**不许** import `gen_nats_conf` —— L0 反向依赖 L1 是分层门禁的硬违规。）

本件是**纯数据 + 纯查询**：零 import、零 IO、零环境依赖，所有函数都是纯函数。
"""

__all__ = [
    "SCHEMA", "ROLE_MEMBER", "ROLES", "CRED_KINDS", "CRED_NODES", "ROSTER",
    "members", "member_names", "entry", "role_of", "cred_spec",
]

#: 档案契约版本（**键名/条目形态冻结**；改形状必须进版本号，并同步推导器与契约文档）
SCHEMA = "l5-roster-archive/1"

#: 本刀唯一的角色名。多角色是将来的事 —— 到那时在这里加值，并在契约文档里写明各角色的模板。
ROLE_MEMBER = "member"
ROLES = (ROLE_MEMBER,)

#: 凭据落地的两类位置（语义见模块文档串；**种类不是路径**）
CRED_RUNTIME = "runtime"
CRED_CONF = "conf"
CRED_KINDS = (CRED_RUNTIME, CRED_CONF)

#: 凭据可能落在哪些节点（节点名，不是地址）。与 `tools/deploy_targets.json` 的 `name` 同名。
CRED_NODES = ("thinkbook", "lobster", "quantum", "top001")

#: 名册本体 —— **有序**表，顺序 == 中枢 conf 里 users 的规范序（字典序）。
#: 每条 `(member, role, cred_node, cred_kind)`。
#: 真值来源：2026-09-27 刀27 真机只读实测（两台中枢用户区逐字相同；每个成员各有唯一凭据文件）。
ROSTER = (
    ("claudecode",  ROLE_MEMBER, "thinkbook", CRED_RUNTIME),
    ("codex",       ROLE_MEMBER, "thinkbook", CRED_RUNTIME),
    ("hermes",      ROLE_MEMBER, "thinkbook", CRED_RUNTIME),
    ("lobster",     ROLE_MEMBER, "lobster",   CRED_CONF),
    ("openclaw-nb", ROLE_MEMBER, "thinkbook", CRED_RUNTIME),
    ("piagent",     ROLE_MEMBER, "thinkbook", CRED_RUNTIME),
    ("quantum",     ROLE_MEMBER, "quantum",   CRED_CONF),
    ("top001",      ROLE_MEMBER, "top001",    CRED_CONF),
)

#: 成员名 → 条目（查询用；顺序由 `ROSTER` 负责，本表不承担顺序语义）
_BY_NAME = {row[0]: row for row in ROSTER}


def members():
    """全体成员名，**按档案顺序**（= 规范序）。调用方拿它去比 conf 时不许再排一次。"""
    return [row[0] for row in ROSTER]


def member_names():
    """`members()` 的别名 —— 与刀25 对账器里 `roster` 那个词对齐，读起来不必猜。"""
    return members()


def entry(name):
    """成员的完整条目 `(member, role, cred_node, cred_kind)`；不在名册返回 `None`（**不猜**）。"""
    return _BY_NAME.get(name)


def role_of(name):
    """成员的角色名；不在名册返回 `None`。"""
    row = _BY_NAME.get(name)
    return row[1] if row else None


def cred_spec(name):
    """成员的凭据**位置说明** `(cred_node, cred_kind)`；不在名册返回 `None`。

    注意返回的是「哪台机 + 哪一类位置」，**不是路径** —— 拼绝对路径要拿 `cred_kind` 去问
    L0 `node_profile`（`runtime_dir()` / `nats_conf_dir_posix()`）与部署表，见
    `tools/roster_derive_conf.py :: cred_path()`。
    """
    row = _BY_NAME.get(name)
    return (row[2], row[3]) if row else None
