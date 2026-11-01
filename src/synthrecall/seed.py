from __future__ import annotations

from sqlalchemy import select

from synthrecall.database import SessionFactory
from synthrecall.models import Category, Paper, PaperDetail, Tag


def seed_demo_data(session_factory: SessionFactory) -> None:
    with session_factory() as session:
        has_categories = session.scalar(select(Category.id).limit(1))
        if has_categories is not None:
            return

        uncategorized = Category(name="未分类", sort_order=0)
        research = Category(name="研究方向", sort_order=10)
        image_cls = Category(
            name="图像分类",
            parent=research,
            preset_keywords="classification, ImageNet, ConvNeXt, ViT",
            sort_order=11,
        )
        detection = Category(
            name="目标检测",
            parent=research,
            preset_keywords="detection, COCO, YOLO, DETR",
            sort_order=12,
        )
        segmentation = Category(
            name="语义分割",
            parent=research,
            preset_keywords="segmentation, Cityscapes, ADE20K, U-Net",
            sort_order=13,
        )

        method = Category(name="方法类型", sort_order=20)
        transformer = Category(
            name="Transformer",
            parent=method,
            preset_keywords="attention, transformer, ViT, Swin",
            sort_order=21,
        )
        pruning = Category(
            name="模型压缩",
            parent=method,
            preset_keywords="pruning, distillation, quantization, low-rank",
            sort_order=22,
        )

        status = Category(name="阅读状态", sort_order=30)
        todo = Category(name="待读", parent=status, sort_order=31)
        deep_read = Category(name="精读", parent=status, sort_order=32)

        tags = [
            Tag(name="survey", color="#64748b"),
            Tag(name="benchmark", color="#0f766e"),
            Tag(name="reproduce", color="#b45309"),
        ]

        demo_paper = Paper(
            title="示例论文：ResearchWeave 如何组织一篇文献",
            authors="ResearchWeave Team",
            year=2026,
            venue="Demo Library",
            abstract=(
                "这是一条示例文献，用来展示标题、摘要、分类、总结、数据集和个人批注"
                "在详情页中的组织方式。"
            ),
            reading_status="精读",
            importance=4,
            categories=[image_cls, transformer, deep_read],
            tags=[tags[0]],
            detail=PaperDetail(
                summary="这里可以保存对全文的中文总结。后续接入大模型后，这部分可以由 AI 生成，再由用户编辑确认。",
                datasets="ImageNet; CIFAR-100",
                methods="分类树; 本地数据库; 文献知识卡片",
                contributions="把论文 PDF、结构化字段和个人批注放在同一个本地工作流里。",
                limitations="当前 Demo 还没有接入真实 PDF 全文索引和大模型。",
                experiment_notes="可在这里记录实验设置、指标、复现结果。",
                reproducibility_notes="可记录代码仓库、环境、运行脚本和踩坑点。",
                personal_notes="这篇示例文献可以用来测试搜索：试着搜“知识卡片”或“ImageNet”。",
            ),
        )

        second_paper = Paper(
            title="示例论文：低秩模型压缩阅读笔记",
            authors="Demo Author",
            year=2025,
            venue="ResearchWeave Notes",
            abstract="这条记录用于测试方法分类、数据集字段和批注搜索。",
            reading_status="待读",
            importance=3,
            categories=[pruning, todo],
            tags=[tags[2]],
            detail=PaperDetail(
                summary="低秩分解可以被记录在方法字段中，方便后续做论文对比。",
                datasets="ImageNet; COCO",
                methods="Low-rank; Pruning; Distillation",
                contributions="展示模型压缩类论文如何进入同一个文献库。",
                personal_notes="后续可以把这个方向扩展成复现管理模块。",
            ),
        )

        session.add_all(
            [
                uncategorized,
                research,
                image_cls,
                detection,
                segmentation,
                method,
                transformer,
                pruning,
                status,
                todo,
                deep_read,
                *tags,
                demo_paper,
                second_paper,
            ]
        )
        session.commit()
