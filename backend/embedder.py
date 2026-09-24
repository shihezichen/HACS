#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
embedder.py —— 本地向量嵌入（fastembed + onnxruntime，无需任何外部 API）

设计要点（与 blackboard_svc.py 同一套口径）：
  * **惰性单例**：模块导入只做环境变量解析，绝不碰模型。第一次 embed_texts()
    才真正加载 —— 服务启动因此不会被几百 MB 的模型加载拖住，更不会被它拖挂。
  * **永不抛异常**：embed_texts() 任何失败（没装 fastembed / 模型缺失 / 下载不通 /
    维度对不上）统一返回 None，由调用方自己决定降级。黑板的可用性不依赖向量。
  * 线程安全：服务是 ThreadingHTTPServer，加载走双重检查 + 一把锁，只做一次；
    **加载失败是粘性的**（记住 _failed，不再重试）—— 否则每个请求都去重试一次
    几秒钟的模型加载，等于把服务拖死。要重试请显式调用 reset()。
  * 第三方依赖只有 fastembed（它自己依赖 onnxruntime/numpy），此外一律标准库。

环境变量：
    BB_EMBED_MODEL   模型名，默认 BAAI/bge-small-zh-v1.5
    BB_EMBED_DIM     期望维度，默认 512（必须与库里 vector(N) 的 N 一致）

对外接口：
    embed_texts(texts) -> list[list[float]] | None
    status()           -> {"available","model","dim","loaded","last_error"}
"""

import os
import threading
from typing import Any, Dict, List, Optional

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_DIM = 512
ERROR_MAX = 200          # last_error 截断长度，与 blackboard_svc 的 DETAIL_MAX 对齐

_LOCK = threading.Lock()
_MODEL = None            # type: Any      加载成功的 TextEmbedding 实例
_LOADED = False          # type: bool     是否已加载成功
_FAILED = False          # type: bool     是否已确证不可用（粘性）
_LAST_ERROR = None       # type: Optional[str]


# ---------------------------------------------------------------- 配置

def model_name() -> str:
    """当前模型名。每次读环境变量，便于测试里临时改。"""
    return (os.environ.get("BB_EMBED_MODEL") or "").strip() or DEFAULT_MODEL


def embed_dim() -> int:
    """期望维度。非法值（空 / 非数字 / <=0）一律退回默认值，不抛。"""
    raw = (os.environ.get("BB_EMBED_DIM") or "").strip()
    if not raw:
        return DEFAULT_DIM
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_DIM
    return value if value > 0 else DEFAULT_DIM


def _short(exc: Any) -> str:
    """异常 -> 可以安全外泄的一行字（模型路径里可能有用户名，只截断不脱敏更多）。"""
    text = str(exc).replace("\n", " ").strip() or exc.__class__.__name__
    return text[:ERROR_MAX]


# ---------------------------------------------------------------- 惰性加载

def _load() -> Any:
    """返回模型实例；不可用时返回 None（并记下 _last_error）。绝不抛。"""
    global _MODEL, _LOADED, _FAILED, _LAST_ERROR
    model = _MODEL
    if model is not None:                    # 快路径：加载过就不进锁
        return model
    with _LOCK:
        if _MODEL is not None:               # 双重检查：可能被别的线程抢先加载了
            return _MODEL
        if _FAILED:                          # 粘性失败：不重试
            return None
        try:
            from fastembed import TextEmbedding
        except Exception as exc:             # 不止 ImportError：onnxruntime 起不来也算
            _FAILED = True
            _LAST_ERROR = "fastembed 导入失败：%s" % _short(exc)
            return None
        try:
            _MODEL = TextEmbedding(model_name=model_name())
        except Exception as exc:             # 模型名不存在 / 权重下载不通 / 磁盘满
            _FAILED = True
            _LAST_ERROR = "模型加载失败（%s）：%s" % (model_name(), _short(exc))
            return None
        _LOADED = True
        _LAST_ERROR = None
        return _MODEL


def reset() -> None:
    """丢掉已加载的模型与失败标记。给测试和"改完环境变量想重试"用。"""
    global _MODEL, _LOADED, _FAILED, _LAST_ERROR
    with _LOCK:
        _MODEL = None
        _LOADED = False
        _FAILED = False
        _LAST_ERROR = None


# ---------------------------------------------------------------- 对外接口

def embed_texts(texts: List[str]) -> Optional[List[List[float]]]:
    """把一批文本编码成向量。

    返回 len(texts) 条、每条 embed_dim() 维的浮点列表；**任何失败都返回 None**，
    不抛异常 —— 调用方据此降级（黑板写入留 NULL、检索退回关键词）。
    空列表原样返回 []，不会因此去加载模型。
    """
    global _LAST_ERROR
    if not isinstance(texts, (list, tuple)):
        _LAST_ERROR = "embed_texts 需要一个列表"
        return None
    if not texts:
        return []
    for item in texts:
        if not isinstance(item, str):
            _LAST_ERROR = "embed_texts 只接受字符串"
            return None

    model = _load()
    if model is None:
        return None

    want = embed_dim()
    try:
        vectors = []  # type: List[List[float]]
        for raw in model.embed(list(texts)):     # fastembed 返回的是生成器
            values = raw.tolist() if hasattr(raw, "tolist") else list(raw)
            vectors.append([float(x) for x in values])
    except Exception as exc:
        _LAST_ERROR = "编码失败：%s" % _short(exc)
        return None

    if len(vectors) != len(texts):
        _LAST_ERROR = "编码结果条数不符（期望 %d，实得 %d）" % (len(texts), len(vectors))
        return None
    for vec in vectors:
        if len(vec) != want:
            # 维度对不上就整批作废：写进 vector(N) 列只会被 PG 拒绝，不如早点降级。
            _LAST_ERROR = ("维度不符：模型给了 %d 维，BB_EMBED_DIM=%d"
                           % (len(vec), want))
            return None
    _LAST_ERROR = None
    return vectors


def status() -> Dict[str, Any]:
    """健康信息。**绝不触发加载** —— /healthz 必须是廉价的。

    available：尚未确证不可用（还没加载过时也是 True，因为确实还没失败过）；
    loaded：模型已经在内存里。两者一起看才能区分"还没用过"和"用过且好使"。
    """
    return {
        "available": not _FAILED,
        "model": model_name(),
        "dim": embed_dim(),
        "loaded": _LOADED,
        "last_error": _LAST_ERROR,
    }
