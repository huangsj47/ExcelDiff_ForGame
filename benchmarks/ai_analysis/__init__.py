#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 分析严格评测基线 —— 固定输入 / 人工金标 / 阶段剖析 / A-B 比较。

见 `dataset.py` 的模块 docstring：这一层交付的是**度量能力**（任务 H），
不含任何 token 优化（那是任务 E）。顺序不能颠倒 —— 没有金标就没有
「召回率没降」这个判据，那时任何 token 下降都会被记成成功。
"""
from __future__ import annotations

from .dataset import (  # noqa: F401
    GOLD_SCHEMA,
    GOLD_SEVERITIES,
    SAMPLE_SCHEMA,
    SEVERITY_ORDER,
    BenchmarkDataset,
    BenchmarkFormatError,
    BenchmarkSample,
    GoldIssue,
    GoldLabel,
    load_dataset,
    normalize_path,
)

__all__ = [
    "GOLD_SCHEMA",
    "GOLD_SEVERITIES",
    "SAMPLE_SCHEMA",
    "SEVERITY_ORDER",
    "BenchmarkDataset",
    "BenchmarkFormatError",
    "BenchmarkSample",
    "GoldIssue",
    "GoldLabel",
    "load_dataset",
    "normalize_path",
]
