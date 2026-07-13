# DOI Extraction Standalone

一个自包含的 PDF DOI 候选提取与元数据校验流水线。它面向批量论文 PDF，优先使用本地可获得的信息提取 DOI 和基础书目信息，并可按需接入 GROBID、Crossref/DOI.org、OCR 和 OpenAI-compatible LLM 做补全与复核。

## 功能概览

- 批量扫描 PDF 目录，支持递归处理。
- 从文件名、PDF 原始字节、XMP 元数据、PDF URI annotation、`pypdf` 元数据和首页文本中提取 DOI。
- 可读取已有 Markdown 文件作为 DOI 候选来源。
- 可读取已有 GROBID 元数据，或在本地/内网 GROBID 服务可用时按需调用 `processHeaderDocument`。
- 可用 Crossref 校验 DOI 与标题/作者是否匹配，并可通过标题搜索补充候选 DOI。
- 可对扫描件或元数据较差的 PDF 启用 OCR，提取首页文本、标题、作者和 DOI 候选。
- 可接入 OpenAI-compatible Chat Completions API 作为最后的 DOI 候选兜底。
- 自动产出 `metadata.jsonl` 和 `metadata.tsv`，并对 `review`/疑难记录做后处理。

> 注意：本项目是 standalone 版本，不包含 PDF 转 Markdown 的转换器。`--markdown-dir` 只会读取你已经准备好的 `.md` 文件。

## 项目结构

```text
.
├── doi_pipeline/
│   ├── pipeline.py              # 主命令入口
│   ├── doi.py                   # DOI 提取、归一化和候选选择
│   ├── metadata.py              # 标题/作者清洗、GROBID 元数据合并、相似度
│   ├── validators.py            # Crossref 校验与缓存
│   ├── grobid.py                # GROBID processHeaderDocument 客户端
│   ├── ocr.py                   # 首页 OCR 与标题/作者提取
│   ├── review_postprocess.py    # review DOI 的 DOI.org 后处理
│   └── hardcase_postprocess.py  # 疑难样本的补充检索与判定
├── tools/                       # 离线复核、OCR、LLM、人工面板等辅助脚本
├── tests/                       # 单元测试
├── pyproject.toml
└── README.md
```

## 安装

建议使用 Python 3.10 及以上版本。

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip
```

基础安装：

```bash
python3 -m pip install -e .
```

推荐安装 PDF 解析依赖：

```bash
python3 -m pip install -e ".[pdf]"
```

如果需要 OCR：

```bash
python3 -m pip install -e ".[pdf,ocr]"
```

OCR 还需要系统中有 Tesseract。若 Python OCR 路径不可用，程序会尝试退回到命令行 `tesseract`；更底层的兜底路径还会用到 `gs`。`eng+chi_sim`、`jpn` 等语言需要对应的 Tesseract 语言包。

安装后可使用两种入口：

```bash
python3 -m doi_pipeline.pipeline -h
doi-pipeline -h
```

## 快速开始

最小可运行命令：

```bash
python3 -m doi_pipeline.pipeline /path/to/pdfs \
  --recursive \
  --output-dir output/run1
```

安装为 console script 后：

```bash
doi-pipeline /path/to/pdfs \
  --recursive \
  --output-dir output/run1
```

快速本地扫描大文件时可启用 `--fast`，大 PDF 只读取前 20 MB：

```bash
python3 -m doi_pipeline.pipeline /path/to/pdfs \
  --recursive \
  --output-dir output/fast_run \
  --fast \
  --workers 8 \
  --no-review-postprocess \
  --no-hardcase-postprocess
```

推荐的联网复核模式：

```bash
export CROSSREF_MAILTO=you@example.com

python3 -m doi_pipeline.pipeline /path/to/pdfs \
  --recursive \
  --output-dir output/full_run \
  --validate \
  --validate-all \
  --workers 8
```

带 GROBID 与 OCR 的完整模式：

```bash
python3 -m doi_pipeline.pipeline /path/to/pdfs \
  --recursive \
  --output-dir output/grobid_ocr_run \
  --grobid-on-missing \
  --grobid-endpoint http://127.0.0.1:8070/api/processHeaderDocument \
  --fallback-allow-ocr \
  --ocr-metadata-on-grobid-fail \
  --ocr-pages 3 \
  --ocr-language eng+chi_sim \
  --validate \
  --validate-all \
  --workers 8
```

## 处理流程

1. 收集 PDF：扫描 `input_dir` 下的 `.pdf` 文件，`--recursive` 开启递归。
2. 本地 DOI 提取：从文件名、原始 PDF 字节、XMP、PDF URI、`pypdf` 元数据和前几页文本中提取 DOI。
3. 元数据合并：优先使用 PDF 元数据；如果提供 `--grobid-metadata` 或开启 `--grobid-on-missing`，再合并 GROBID 的标题、作者、年份、期刊、出版社和摘要。
4. Markdown 候选：如果提供 `--markdown-dir`，读取同名 Markdown 中的 DOI。匹配文件名为 `<pdf_stem>.md` 或 `<pdf_filename>.md`。
5. 候选选择：多独立来源支持的 DOI 会升级为 high；仅来自文件名的 DOI 在默认策略下进入 `review`。
6. Crossref 校验：`--validate` 会校验低置信度或 review DOI；`--validate-all` 会校验全部 DOI。
7. OCR 兜底：`--fallback-allow-ocr` 打开后，会在无 DOI 或 review 时从 PDF 首页 OCR 文本里继续找候选；配合 `--ocr-metadata-on-grobid-fail` 可用 OCR 填标题/作者。
8. LLM 兜底：`--llm` 打开后，在缺 DOI 或 review 时调用 OpenAI-compatible 接口生成候选 DOI，再交给校验逻辑。
9. 后处理：默认会对非 `ok` 记录执行 `review_postprocess` 和 `hardcase_postprocess`，尝试通过 DOI.org/Crossref/DataCite 等权威源确认或纠正 DOI。

## 命令参数

### 基础输入输出

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `input_dir` | 必填 | PDF 所在目录 |
| `--output-dir PATH` | 必填 | 输出目录；不存在会自动创建 |
| `--recursive` | 关闭 | 递归扫描子目录 |
| `--workers N` | `min(8, CPU)` | PDF 并发处理 worker 数 |
| `--fast` | 关闭 | 对大 PDF 只读取前 20 MB，速度更快但可能漏掉尾部 DOI |

当前版本没有 `--resume`。同一个 `--output-dir` 下的 `metadata.jsonl` 和 `metadata.tsv` 会在运行结束时重写；缓存文件会复用和追加。

### 候选来源

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--markdown-dir PATH` | 空 | 读取已有 Markdown 文件中的 DOI 候选，不负责 PDF 转 Markdown |
| `--filename-doi-policy candidate` | `candidate` | 仅文件名 DOI 默认需要复核；设为 `trusted` 可直接按来源权重接受 |

### GROBID

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--grobid-metadata PATH` | 空 | 读取已有 GROBID TSV/JSONL 元数据 |
| `--grobid-on-missing` | 关闭 | 当标题/作者缺失时调用 GROBID endpoint |
| `--grobid-endpoint URL` | 空 | GROBID `processHeaderDocument` endpoint |
| `--grobid-timeout SECONDS` | `180` | 单个 GROBID 请求超时 |

`--grobid-metadata` 支持 TSV 或 JSONL。记录中建议包含 `source_pdf`、`filename` 或 `path`，用于按 PDF 文件名匹配；可选字段包括 `title`、`authors`、`doi`、`year`、`journal`、`publisher`、`abstract`。

### Crossref 与权威源校验

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--validate` | 关闭 | 对低置信度或 review DOI 做 Crossref 标题/作者校验 |
| `--validate-all` | 关闭 | 对全部 DOI 候选做 Crossref 校验 |
| `--crossref-mailto EMAIL` | `$CROSSREF_MAILTO` | 写入 User-Agent，使用 Crossref polite pool |
| `--no-review-postprocess` | 开启后处理 | 关闭 review-row 后处理 |
| `--no-review-authority` | 开启权威查询 | 后处理时不查 DOI.org |
| `--authority-workers N` | `8` | DOI.org 后处理并发数 |
| `--authority-timeout SECONDS` | `20` | DOI.org 请求超时 |
| `--no-hardcase-postprocess` | 开启疑难后处理 | 关闭疑难记录后处理 |
| `--no-hardcase-title-search` | 开启标题搜索 | 疑难后处理时不做 Crossref/DataCite 标题搜索 |
| `--hardcase-workers N` | `6` | 疑难后处理并发数 |
| `--hardcase-timeout SECONDS` | `15` | 疑难后处理请求超时 |
| `--hardcase-pdf-pages N` | `4` | 疑难后处理读取 PDF 首页页数 |

如果需要完全离线运行，建议不要使用 `--validate`、`--validate-all`、`--grobid-on-missing`、`--llm`，并加上：

```bash
--no-review-postprocess --no-hardcase-postprocess
```

### OCR

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--fallback-allow-ocr` | 关闭 | 允许 OCR 作为 DOI/元数据兜底 |
| `--ocr-metadata-on-grobid-fail` | 关闭 | GROBID 失败或标题/作者缺失时用 OCR 补元数据 |
| `--ocr-pages N` | `3` | OCR PDF 首页页数 |
| `--ocr-language LANG` | `eng+chi_sim` | Tesseract 语言表达式 |

### LLM

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--llm` | 关闭 | 启用 LLM DOI 候选兜底 |
| `--llm-base-url URL` | `$OPENAI_BASE_URL` | OpenAI-compatible base URL，不含 `/chat/completions` |
| `--llm-model NAME` | `$OPENAI_MODEL` | 模型名称 |
| `--llm-api-key KEY` | `$OPENAI_API_KEY` | API key |
| `--llm-timeout SECONDS` | `120` | LLM 请求超时 |

示例：

```bash
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_MODEL=gpt-4.1
export OPENAI_API_KEY=sk-...

python3 -m doi_pipeline.pipeline /path/to/pdfs \
  --output-dir output/llm_run \
  --llm \
  --validate
```

## 输出文件

主流水线输出在 `--output-dir` 下：

| 文件 | 说明 |
|------|------|
| `metadata.jsonl` | 主结果，每个 PDF 一行 JSON |
| `metadata.tsv` | 人类可读汇总表，字段顺序由 `SUMMARY_FIELDS` 定义 |
| `metadata_validation_cache.jsonl` | Crossref DOI/标题校验缓存，仅在 `--validate` 或 `--validate-all` 时产生 |
| `doi_authority_cache.jsonl` | DOI.org authority 查询缓存，review 后处理使用 |
| `hardcase_authority_cache.jsonl` | 疑难后处理缓存 |
| `review_postprocess_summary.json` | review 后处理统计 |
| `hardcase_postprocess_summary.json` | 疑难后处理统计 |

运行过程中会按完成顺序打印：

```text
3/120    ok      10.1021/acsami.5c04087    paper.pdf
```

最后会打印一个 JSON 摘要，包含总行数、耗时、输出文件路径和后处理统计。

### 状态字段

| `status` | 含义 |
|----------|------|
| `ok` | DOI 已被接受 |
| `review` | 找到了 DOI 候选，但置信度或证据不足，需要复核 |
| `no_doi` | 正常处理完成，但未找到 DOI |
| `error` | 处理异常，错误信息写入 `error` |

### 常用列

| 列 | 说明 |
|----|------|
| `filename` / `path` | PDF 文件名和路径 |
| `doi` / `confidence` | 选中的 DOI 和置信度 |
| `method` / `doi_decision` | DOI 选择原因 |
| `doi_source` | 选中 DOI 的来源，如 `xmp_tag_prism:doi_0`、`pypdf_page_1`、`filename_doi` |
| `metadata_source` | 元数据来源组合，如 `pdf`、`pdf+grobid`、`pdf+ocr` |
| `title` / `authors` / `year` / `journal` / `publisher` / `abstract` | 书目字段 |
| `grobid_doi` / `grobid_title` / `grobid_authors` | GROBID 提供的候选字段 |
| `ocr_title` / `ocr_authors` / `ocr_error` | OCR 结果 |
| `doi_validation_source` / `doi_validation_score` | Crossref 校验来源和分数 |
| `doi_candidates` | 所有候选 DOI 的 JSON 数组 |
| `original_*`、`postprocess_*`、`authority_*`、`hardcase_*` | 后处理过程中的审计字段 |

## 常见运行模式

### 纯本地离线扫描

```bash
python3 -m doi_pipeline.pipeline /data/papers \
  --recursive \
  --output-dir output/offline \
  --workers 8 \
  --no-review-postprocess \
  --no-hardcase-postprocess
```

### 读取已有 Markdown 候选

```bash
python3 -m doi_pipeline.pipeline /data/papers \
  --recursive \
  --markdown-dir /data/paper_markdown \
  --output-dir output/with_markdown \
  --validate
```

Markdown 文件命名需要匹配 PDF：

```text
/data/papers/example.pdf
/data/paper_markdown/example.md
# 或
/data/paper_markdown/example.pdf.md
```

### 复用已有 GROBID 结果

```bash
python3 -m doi_pipeline.pipeline /data/papers \
  --recursive \
  --grobid-metadata output/grobid_headers.tsv \
  --output-dir output/with_grobid_metadata \
  --validate
```

### 扫描件或低文本 PDF

```bash
python3 -m doi_pipeline.pipeline /data/scanned_papers \
  --recursive \
  --output-dir output/ocr \
  --fallback-allow-ocr \
  --ocr-metadata-on-grobid-fail \
  --ocr-pages 3 \
  --ocr-language eng+chi_sim \
  --validate
```

## 辅助工具

`tools/` 下的脚本主要用于大规模批处理后的二次复核。它们都要求显式指定输出目录。

| 脚本 | 用途 |
|------|------|
| `tools/build_manual_review_panel.py` | 从未解决 TSV 构建人工复核 HTML 面板 |
| `tools/resolve_with_ocr.py` | 对低文本/扫描件行单独跑 OCR 并做权威源校验 |
| `tools/resolve_with_llm_markdown.py` | 将 PDF 前几页转文本/Markdown 后交给 LLM 抽取候选 |
| `tools/resolve_hard_cases.py` | 对疑难清单做多源 DOI authority 解析 |
| `tools/resolve_remaining_143.py` | 历史批次剩余样本的二轮解析脚本 |

示例：生成人工复核面板。

```bash
python3 tools/build_manual_review_panel.py output/full_run/metadata.tsv \
  --output-dir output/manual_review \
  --local-base /data/papers
```

示例：对低文本记录单独 OCR。

```bash
python3 tools/resolve_with_ocr.py output/full_run/metadata.tsv \
  --output-dir output/ocr_followup \
  --local-base /data/papers \
  --workers 2
```

## 测试

```bash
python3 -m pytest
```

如果没有安装 `pytest`：

```bash
python3 -m pip install pytest
```

## 使用建议

- 大批量任务建议每次使用新的 `--output-dir`，便于保留审计结果。
- 需要联网校验时设置 `CROSSREF_MAILTO`，对 Crossref 更友好，也更稳定。
- OCR 成本较高，建议先跑普通流程，再对 `review`/`no_doi` 子集单独启用 OCR。
- 如果只想要最保守、可审计的结果，关注 `metadata.tsv` 中 `status=ok` 且 `confidence=high` 的记录。
- 默认后处理会尝试确认更多疑难记录；如果你只想看主提取结果，可关闭 `--no-review-postprocess --no-hardcase-postprocess`。
