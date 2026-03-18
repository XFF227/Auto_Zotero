# Paper Workflow

一个面向计算生物学文献筛选、Zotero 管理、浅摘要与 deep summary 的本地工具。

支持两种使用方式：
- GUI：Streamlit 本地页面
- CLI：`pipeline.py`

## 功能概览

- 抓取论文标题：
  - `bioRxiv`
  - `arXiv`
  - `PubMed`
- 支持按 `TARGET_JOURNALS` 限定 `PubMed`
- 支持 DOI 直接导入
- 标题筛选仅使用标题，不读取 PDF 全文
- 浅摘要与 deep summary 仅使用 `title + abstract`
- 选中文章后可直接保存到 Zotero，并尽量附带 PDF
- shallow / deep 页面优先从本地 Zotero library 读取

## 目录结构

```text
project/
├── app.py
├── pipeline.py
├── config.py
├── prompts.py
├── run_app.command
├── run_app_windows.bat
├── README.md
├── .gitignore
├── modules/
├── data/
├── output/
└── pdfs/
```

## 安装

### 1. Python

建议使用 Python 3.11 或 3.12。

### 2. GUI 依赖

```bash
python3 -m pip install -r requirements-gui.txt
```

Windows:

```bat
python -m pip install -r requirements-gui.txt
```

## 启动方式

### macOS

双击：

- `run_app.command`

或者命令行：

```bash
python3 -m streamlit run app.py
```

### Windows

双击：

- `run_app_windows.bat`

或者命令行：

```bat
python -m streamlit run app.py
```

## GUI 使用流程

### 1. 检索标题

- 在左侧填写参数
- 可直接点击“开始检索”
- 也可输入 DOI 列表后点击“导入 DOI”
- 在结果列表中勾选文章
- 点击“保存选中文章到 Zotero”

这一步会：
- 创建当天 Zotero 子文件夹
- 导入 metadata
- 尽量下载并上传 PDF

### 2. 浅摘要

- 进入“浅摘要 + Zotero”
- 选择本地 Zotero 子文件夹
- 点击“列出本地条目”
- 勾选文章
- 点击“生成浅摘要并写回 Zotero”

### 3. Deep Summary

- 进入“Deep Summary”
- 选择本地 Zotero 子文件夹
- 点击“列出子文件夹标题”
- 勾选文章
- 点击“生成 Deep Summary 并写回 Zotero”

## CLI 用法

### 常规抓取

```bash
python3 pipeline.py
```

### DOI 导入

```bash
python3 pipeline.py --doi 10.1038/s41592-026-03046-5 10.xxxx/xxxx
```

或：

```bash
python3 pipeline.py --doi
```

### 保存候选到 Zotero

```bash
python3 pipeline.py --save-zotero 1 3 5
```

### 本地 Zotero 浅摘要

```bash
python3 pipeline.py --zotero-shallow PZYKWZP7 1 2
```

### 本地 Zotero deep summary

```bash
python3 pipeline.py --zotero-deep PZYKWZP7 1 2
```

## Prompt 与缓存

Prompt 在：

- `prompts.py`

业务缓存默认写在：

- `data/cache.json`

如果你改了 prompt 但结果没变，通常是因为命中了业务缓存，而不是 GUI 的网页缓存。

GUI 左侧现在有一个按钮：

- `清空业务缓存`

它会清掉：
- `title_screening`
- `shallow_summaries`
- `deep_summaries`

## 本地 Zotero 说明

当前本地 Zotero 路径由下面这个配置控制：

- `ZOTERO_LOCAL_DIR`

默认是：

- `/path/to/Zotero`

deep / shallow 页面会优先从本地 `zotero.sqlite + storage` 读取。


