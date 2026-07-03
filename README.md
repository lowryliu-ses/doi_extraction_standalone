# PDF 元数据提取

从论文 PDF 批量提取 **DOI 与完整书目元数据**,并记录提取方式及 PDF ↔ Markdown ↔ DOI 对应关系。

核心入口: [`pdf_metadata/batch_extract_metadata_unified.py`](pdf_metadata/batch_extract_metadata_unified.py) — 统一流水线(PDF 直提优先,Markdown 按需回退)。

## 最新整理结果

当前最新 DOI 清洗与复核结果统一保存在:

```text
output/latest_doi_resolution_20260630/all_paper_latest_doi_summary_20260701/
```

最新保守口径统计:

| 指标 | 数值 |
|------|------|
| 总 PDF 数 | `132772` |
| DOI OK | `132731` |
| 剩余待人工复核/未确认 | `41` |
| OK 率 | `99.9691%` |

另有 `3` 篇为 `probable_medium`,单独列出,暂不计入保守 OK。

关键文件:

| 文件 | 说明 |
|------|------|
| `output/latest_doi_resolution_20260630/all_paper_latest_doi_summary_20260701/all_paper_latest_summary.tsv` | 所有 paper 目录最新分组统计,以此为准 |
| `output/latest_doi_resolution_20260630/all_paper_latest_doi_summary_20260701/all_paper_latest_summary.json` | 最新统计 JSON |
| `output/latest_doi_resolution_20260630/all_paper_latest_doi_summary_20260701/remaining47_latest_review.tsv` | 剩余 47 的最终复核状态 |
| `output/latest_doi_resolution_20260630/accepted/newly_accepted_all_41.tsv` | 最近阶段新增确认 DOI |
| `output/latest_doi_resolution_20260630/remaining47_pdfs/files/` | 剩余 47 篇 PDF,未压缩 |
| `output/latest_doi_resolution_20260630/manual_review_remaining47/index.html` | 人工复核面板 |
| `output/latest_doi_resolution_20260630/history/` | 清理前保留的轻量历史审计表 |

## 功能概览

| 字段 | 含义 |
|------|------|
| `method` | DOI 获取方式 |
| `doi_source` | DOI 的细粒度来源(如 XMP、文本流、Markdown 首页) |
| `metadata_source` | 书目字段来源(如 `pdf`、`pdf+crossref`) |
| `pdf_path` / `markdown_path` / `doi` | PDF ↔ Markdown ↔ DOI 对应关系 |

### 三阶段流程

```
阶段1  PDF 规则直提 (快,本地)
  └─ DOI 置信度 ≥ 阈值 → method=pdf_direct,跳过转换

阶段2  仅对未达标 PDF → 转 Markdown → 从 Markdown 提 DOI
  └─ 命中 → method=markdown

阶段3  可选 Crossref / LLM 补全书目字段
  └─ 无 DOI 时 Crossref 标题检索 → method=crossref_title
  └─ LLM 兜底 → method=llm
```

### `method` 取值

| 值 | 说明 |
|----|------|
| `pdf_direct` | 阶段1 从 PDF 直接规则提取,置信度达标 |
| `markdown` | 阶段2 转 Markdown 后提取 |
| `crossref_title` | 阶段3 通过 Crossref 标题搜索得到 DOI |
| `llm` | 阶段3 LLM 兜底 |
| `none` | 未找到 DOI |

## 安装

```bash
cd pdf_extract

# 基础依赖(阶段1 直提,推荐必装)
pip install -r requirements.txt
```

| 运行模式 | 额外依赖 |
|----------|----------|
| 阶段1 直提 | `pypdf`(已包含在 requirements.txt) |
| 阶段2 Markdown 回退(默认开启) | `pip install -r pdf_to_markdown/requirements.txt` |
| 扫描件 OCR 回退 | 同上 + `--fallback-allow-ocr` |

仅做最快直提、不需要回退时,使用 `--no-markdown-fallback`,只需 `pypdf`。

## 快速开始

```bash
# 小规模测试
python3 pdf_metadata/batch_extract_metadata_unified.py pdf_test --limit 10

# 生产推荐(大规模)
python3 pdf_metadata/batch_extract_metadata_unified.py /path/to/pdfs \
  --recursive \
  --output-dir /path/to/output/run1 \
  --workers 8 \
  --convert-workers 4 \
  --accept-confidence medium \
  --crossref \
  --resume

# 仅 PDF 直提(最快,不转 Markdown)
python3 pdf_metadata/batch_extract_metadata_unified.py /path/to/pdfs \
  --recursive --no-markdown-fallback --fast

# 查看全部参数
python3 pdf_metadata/batch_extract_metadata_unified.py -h
```

## 命令行参数

### 输入 / 输出

| 参数 | 默认 | 说明 |
|------|------|------|
| `input_dir` | (必填) | PDF 所在目录 |
| `--output-dir` | `output/pdf_metadata/unified/<目录名>_<时间戳>/` | 输出目录 |
| `--recursive` | 关 | 递归扫描子目录 |
| `--limit N` | 无 | 只处理排序后的前 N 个 PDF |
| `--resume` | 关 | 跳过 `metadata.jsonl` 中已完成的记录(`status=ok` 或 `no_doi`) |

### 阶段1: PDF 直提

| 参数 | 默认 | 说明 |
|------|------|------|
| `--accept-confidence` | `medium` | 直提 DOI 置信度阈值:`high` / `medium` / `low` |
| `--fast` | 关 | 大 PDF 只读头尾,跳过完整流解码(更快,略降准确率) |
| `--workers` | `min(8, CPU)` | 并发 worker 数 |

置信度规则(来自多来源加权):

- `high`: 来源权重 ≥ 90,或同一 DOI 出现 ≥ 3 次
- `medium`: 权重 ≥ 70,或出现 ≥ 2 次
- `low`: 其余有 DOI 的情况

### 阶段2: Markdown 回退

| 参数 | 默认 | 说明 |
|------|------|------|
| `--markdown-fallback` | 开 | 启用 Markdown 回退 |
| `--no-markdown-fallback` | — | 禁用阶段2 |
| `--fallback-allow-ocr` | 关 | 回退时允许 OCR(扫描件,昂贵) |
| `--convert-workers` | `min(4, CPU)` | 阶段2 最大并发转换数 |
| `--front-lines` | `150` | Markdown 首页行数(DOI 优先区) |
| `--sample-pages` | `5` | PDF 类型检测采样页数 |
| `--min-chars-per-page` | `80` | 判定文本页的字符阈值 |
| `--engine` | `rapidocr` | OCR 引擎:`rapidocr` / `tesseract` |
| `--lang` | `eng+chi_sim` | Tesseract 语言 |
| `--dpi` | `300` | OCR 渲染 DPI |

### 阶段3: 补全

| 参数 | 说明 |
|------|------|
| `--crossref` | 按 DOI 从 Crossref 补全 title/authors/year/journal 等 |
| `--llm` | LLM 兜底(弱/缺 DOI 时) |
| `--llm-base-url` / `--llm-model` / `--llm-api-key` 等 | LLM 配置(也可写 `.env`) |

### 环境变量

复制 [`pdf_metadata/.env.example`](pdf_metadata/.env.example) 为 `pdf_metadata/.env`:

```bash
# Crossref(建议配置,进入礼貌池)
export CROSSREF_MAILTO=you@example.com

# LLM(--llm 时使用)
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_MODEL=gpt-4.1
```

## 输出文件

默认输出目录: `output/pdf_metadata/unified/<input_dir名>_<时间戳>/`

```
output/pdf_metadata/unified/pdfs_20260619_184500/
├── metadata.jsonl      # 主结果,每 PDF 一行 JSON(增量写入,可 resume)
├── metadata.tsv        # 人类可读汇总表
├── mapping.tsv         # PDF ↔ Markdown ↔ DOI 对应关系
├── markdown/           # 阶段2 按需生成的 .md(仅回退成功的)
├── failed.tsv          # 仅含 status=error 的记录(no_doi 不算失败)
├── errors.log          # 转换/Crossref/LLM 等非致命错误(带时间戳)
└── progress.log        # 运行进度
```

> 运行结束会分别统计 `OK` / `No DOI` / `Errors`;进程退出码仅在出现 `error` 时为非 0,`no_doi` 视为正常完成。

### `metadata.tsv` 主要列

| 列 | 说明 |
|----|------|
| `filename` / `path` | 文件名 / 绝对路径 |
| `status` | `ok`(找到 DOI) / `no_doi`(处理正常但无 DOI) / `error`(处理异常) |
| `method` | DOI 获取方式 |
| `doi` / `confidence` | DOI 及置信度 |
| `doi_source` | 如 `xmp_tag_prism:doi`、`pdf_text_stream`、`markdown_front` |
| `metadata_source` | 如 `pdf`、`pdf+crossref`、`pdf+crossref+llm` |
| `markdown_path` | 回退转换的 Markdown 路径(无则为空) |
| `markdown_converter` | 转换器:`markitdown` / `pymupdf` / `ocr` |
| `pdf_type` | `text` / `image` / `mixed` |
| `title` / `authors` / `year` / `journal` / … | 完整书目字段 |

### `mapping.tsv`

```
pdf_path    markdown_path    doi    method    confidence
```

用于建立 **原始 PDF → Markdown → DOI** 的一一对应。

### `metadata.jsonl` 示例(字段节选)

```json
{
  "path": "/data/papers/10.1021_acsami.5c04087.pdf",
  "doi": "10.1021/acsami.5c04087",
  "confidence": "high",
  "method": "pdf_direct",
  "doi_source": "xmp_tag_prism:doi",
  "metadata_source": "pdf+crossref",
  "markdown_path": "",
  "title": "...",
  "authors": ["..."],
  "year": 2025,
  "journal": "...",
  "status": "ok"
}
```

## 十万级 PDF 推荐策略

分两批跑,控制成本与网络:

```bash
# 第一批:直提 + Markdown 回退,暂不 Crossref(快)
python3 pdf_metadata/batch_extract_metadata_unified.py /data/papers \
  --recursive --output-dir output/batch1 \
  --workers 8 --convert-workers 4 --resume

# 第二批:加 Crossref 补全(同一 output-dir + --resume)
python3 pdf_metadata/batch_extract_metadata_unified.py /data/papers \
  --recursive --output-dir output/batch1 \
  --crossref --resume
```

建议:

- 默认 `--accept-confidence medium`,多数 PDF 留在阶段1,不转 Markdown
- OCR 默认关闭;仅对扫描件子集单独开 `--fallback-allow-ocr`
- 务必用固定 `--output-dir` + `--resume`,中断可续跑
- Crossref 建议设 `CROSSREF_MAILTO`,走礼貌池、有节流与 DOI 缓存

## 项目结构

```
pdf_extract/
├── pdf_metadata/
│   ├── batch_extract_metadata_unified.py   # 统一流水线(推荐)
│   ├── batch_extract_pdf_metadata.py       # 仅从 PDF 批量提元数据
│   ├── batch_extract_markdown_doi.py       # 仅从 Markdown 批量提 DOI
│   ├── batch_extract_markdown_metadata.py  # 从 Markdown 批量提元数据(+Crossref)
│   ├── batch_common.py                     # 批处理共用逻辑(并发主循环/resume/参数等)
│   ├── extract_pdf_doi.py                  # 单文件 PDF 元数据/DOI(核心库)
│   └── extract_markdown_doi.py             # 单文件 Markdown DOI
├── pdf_to_markdown/
│   ├── batch_convert_pdf_auto.py           # 批量 PDF→Markdown
│   └── pdf_convert_auto.py                 # 单文件自动转换
├── pdf_test/                               # 测试 PDF
└── requirements.txt
```

## 其他脚本

| 脚本 | 用途 |
|------|------|
| `pdf_metadata/batch_extract_pdf_metadata.py` | 仅从 PDF 批量提元数据(无 Markdown 回退、无 method 记录) |
| `pdf_metadata/batch_extract_markdown_doi.py` | 仅从 Markdown 批量提 DOI(支持 `--mapping mapping.jsonl`) |
| `pdf_metadata/batch_extract_markdown_metadata.py` | 从 Markdown 批量提 DOI 并按 DOI 经 Crossref 补全书目字段(支持 `--mapping`、`--fallback-pdf-doi`) |
| `pdf_metadata/extract_pdf_doi.py` | 单文件 PDF 元数据/DOI(也是各批处理共用的核心库) |
| `pdf_metadata/extract_markdown_doi.py` | 单文件 Markdown DOI |
| `pdf_to_markdown/batch_convert_pdf_auto.py` | 批量 PDF→Markdown(产出 `mapping.jsonl`) |

若已有 `pdf_to_markdown` 的 `mapping.jsonl`,也可单独跑 Markdown DOI:

```bash
python3 pdf_metadata/batch_extract_markdown_doi.py \
  --mapping output/pdf_to_markdown/auto/xxx/mapping.jsonl
```

单 PDF 快速查看:

```bash
python3 pdf_metadata/extract_pdf_doi.py paper.pdf --json
python3 pdf_metadata/extract_pdf_doi.py paper.pdf --crossref
```

## 常见问题

**阶段2 报错 `markitdown` / `pymupdf` 未安装?**
安装 `pip install -r pdf_to_markdown/requirements.txt`,或改用 `--no-markdown-fallback`。

**有 DOI 但 `confidence=low`,没走 Markdown?**
低置信度会触发阶段2;若转换失败,看 `errors.log` 的 `convert:` 行。也可提高要求:`--accept-confidence high`。

**`--resume` 如何生效?**
必须使用**同一个** `--output-dir`,且该目录下已有 `metadata.jsonl`。已完成(`status=ok` 或 `no_doi`)的 `path` 会被跳过;`error` 记录会重跑。
