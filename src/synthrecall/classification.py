from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CategoryRule:
    parent: str
    name: str
    keywords: tuple[str, ...]


DEFAULT_CATEGORY_RULES: tuple[CategoryRule, ...] = (
    CategoryRule(
        parent="研究主题",
        name="视觉 Transformer",
        keywords=(
            "vision transformer",
            "transformer",
            "vit",
            "swin",
            "transnext",
            "self-attention",
            "foveal",
            "retentive network",
            "rmt",
        ),
    ),
    CategoryRule(
        parent="研究主题",
        name="动态卷积",
        keywords=(
            "dynamic convolution",
            "dynamic filter",
            "kernelwarehouse",
            "involution",
            "learned queries",
            "attention map",
            "动态卷积",
            "动态滤波",
            "动态生成",
        ),
    ),
    CategoryRule(
        parent="研究主题",
        name="低秩与压缩",
        keywords=(
            "low-rank",
            "low rank",
            "rank-reduced",
            "pruning",
            "distillation",
            "decomposition",
            "factorization",
            "低秩",
            "剪枝",
            "蒸馏",
        ),
    ),
    CategoryRule(
        parent="研究主题",
        name="大卷积核",
        keywords=(
            "large kernel",
            "re-parameterization",
            "reparameterization",
            "structural re-parameterization",
            "large selective kernel",
            "大卷积核",
            "结构重参数",
        ),
    ),
    CategoryRule(
        parent="研究主题",
        name="数据集与基准",
        keywords=(
            "dataset",
            "benchmark",
            "cityscapes",
            "ade20k",
            "coco",
            "imagenet",
            "数据集",
            "基准",
            "指标",
        ),
    ),
    CategoryRule(
        parent="研究主题",
        name="语义分割",
        keywords=(
            "segmentation",
            "semantic segmentation",
            "mask",
            "segformer",
            "u-net",
            "分割",
        ),
    ),
    CategoryRule(
        parent="研究主题",
        name="卷积网络",
        keywords=(
            "convolution",
            "convnext",
            "cnn",
            "convolutional",
            "resnet",
            "卷积",
        ),
    ),
    CategoryRule(
        parent="阅读状态",
        name="待整理",
        keywords=(
            "todo",
            "待读",
            "待整理",
        ),
    ),
)
