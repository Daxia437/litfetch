---
name: lit-fetch
description: "获取学术文献 PDF（按引用清单批量）、**诊断为什么会失败**、区分**预印本/接收稿/正式版**、并把人工下载的文件核验入库。v3.3：随 wheel 分发（`litfetch skill` 写出 / `litfetch skill --check` 校验），与包同一版本号（纯标准库；标题块邻接核验；评分式 title→DOI；按域名快速失败；provenance 版本台账）。v3.4：`litfetch classify` 按「本机能否自动下载」给整份清单逐篇定性。Use when the user wants to download papers by title/DOI/reference list, fill an OA gap, find out why a publisher blocks them, tell a preprint from the version of record, verify/ingest hand-downloaded PDFs, or triage a reference list by reachability (auto-fetchable here vs needs a browser/subscription)."
description_zh: "批量获取文献、诊断失败原因、区分版本（预印本/接收稿/正式版）、人工下载后核验入库"
description_en: "Batch-fetch academic PDFs, diagnose failures, label the version, and ingest hand downloads"
version: 3.3.0
allowed-tools: Read,Write,Bash,Glob,Grep
metadata:
  requires:
    bins: [python3]
    python: [pymupdf]
  optional:
    python: [curl_cffi, websocket-client]
---

# lit-fetch v3.2 — 调用可移植包

逻辑全在 `litfetch` 包里（`core` 无网络、`io` 走网络；`pymupdf` 必需，`curl_cffi`/`websocket-client` 可选）。
**本文件随 wheel 一起分发**：`litfetch skill` 写出、`litfetch skill --check` 校验，所以 skill 不会和 CLI 漂移
（曾经漂过：skill 3.0 / 包 1.2.1）。本文件只写**何时用、怎么调、看到什么结果该做什么**。

```bash
litfetch doctor            # 已安装；未安装则 python -m litfetch（PYTHONPATH=<repo>/tools/litfetch）
litfetch diagnose  --doi DOI --title "标题"     # 为什么下不下来
litfetch resolve   --title "标题" --rank       # 标题→DOI 评分候选，看它凭什么选中
litfetch discover  --title "标题" --web        # OA 索引之外：作者主页/个人网站
litfetch classify  refs.csv --plan plan.csv --known-broken C040   # 逐篇定性：本机能否自动下
litfetch classify  refs.csv --fetch --library <库> --layer L0     # 只自动下 AUTO 那批（同样过核验）
litfetch fetch     refs.json --library <库> --layer L14 -v --budget 90 --log /tmp/a.jsonl
litfetch versions  --library <库>            # 预印本 / 接收稿 / 正式版各多少
litfetch import    <文件.pdf> --library <库> --title "它应该是的那篇" --year 1994
litfetch import    --from-dir ~/Downloads/papers --refs refs.json --library <库> --layer L14
litfetch ledger    --library <库> --bibliography <综述.md> --overrides reading_status.json --write <台账>
litfetch audit     --library <库> --bibliography <综述.md> [--report <报告.md>]
```

退出码：`0` 正常 · `1` 有文件未通过核验 · `2` 环境/参数不完整。

## 第一条纪律：**先诊断，再重试**

"下不下来"不是诊断。2026-10-22 在**校园网**（权限无问题）实测：失败几乎全是**机器人检测**，重试与换代理都不解决。

| `diagnose` 判定 | 含义 | 动作 |
|---|---|---|
| `cloudflare` | "Just a moment…" 中间页 | **别重试**；自己浏览器打开 → `import` |
| `botwall` | Incapsula/Imperva/Distil 壳（约 1 KB） | 同上 |
| `challenge` | 验证页 | 同上 |
| `paywall` | 401/402 或 "purchase this article" | 本网络无权限：图书馆代理或文献传递 |
| `js_required` | 502 / JS 壳 / 大 HTML 页 | `--browser`，或人工下载 |
| `dead` | 404/410 | 换副本（OpenAlex / Semantic Scholar / 作者主页） |
| `not_pdf` | 落页、登录跳转、TDM token 报错 | 看页面再决定 |

**fetch 会消费诊断**：某域名一旦判定为上述墙，同一篇里该域名的其余候选直接跳过（`skipped … host blocked earlier`）。
这不是优化，是修 bug——第一批任务曾在一堵永远不会开的墙上耗了 7 分钟。

**一个实测结论，别浪费时间去"伪装 TLS"**：用 `curl_cffi` 复刻 Chrome 的 TLS 指纹，Wiley/Oxford 仍是
403 "Just a moment…"、Project Euclid 仍是 1 KB 壳。Cloudflare 靠 JS 执行判定，不靠 TLS 记录。
所以 `[tls]` 只在**响应很小且不是 bot 墙**时重试一次（400 与 >32 KB 的 HTML 直接不重试）。

## 第二条纪律：**ID 便宜，信任昂贵**

2026-10-22 两次从记忆里取 arXiv ID：`1802.05438` 实际是《Mean Field MARL》、`1812.11103` 是《Learning to Walk》——都不是 QMIX / PlaNet。
`verify_id` 在**下载前**用标题核对 ID。**按标题查，不按记忆填。**

## 第三条纪律：**核验按标题块 + 词序，不按词袋**

词袋覆盖率会被两类真实错误骗到 1.00：**续作**（*Online* vs *Neural* Relational Inference）与**引用句**（正文印着被引标题）。
取**第 1 页最大字号标题块**（排除 arXiv 印章 / doi 行 / 数字行），要求标题词**连续出现** ≥ max(3, 0.6×词数)。

标题→DOI 同样按分数，不按搜索第一条：`0.5×词 F1 + 0.5×连续词序`，阈值 0.9。
**开头多一个实词 = 另一篇**（`Conditional neural relational inference…` 曾拿 1.00），
**开头多一个虚词 = 同一篇**（`A sparsity-based model…`）。实测：真论文 1.00、续作 0.31、`Conditional…` 0.42。

## 第三条半纪律：**OA 索引不是全部，作者主页要单独找**

OpenAlex / Unpaywall 索引的是机构库，**不索引个人主页**，所以一大批老论文对脚本"看起来不存在"。
`discover` 收集候选（OpenAlex / Semantic Scholar / 可选搜索引擎）并逐个探测，输出可直接粘进 `refs.json` 的 `urls`。
`fetch` 把 `urls` 放在 **LA 层，排在 doi.org 之前**——付费墙论文里，作者自己那份比出版社那份更容易拿到。

**搜索引擎这一层经常不可用**（代理拦截、bot 检查），命令会明说 `search unreachable (...)`，而不是返回空列表假装没有。
人工/agent 搜到 URL 再交回来，是**正规路径**，不是权宜之计——机器负责核验与命名，人负责它擅长的那一步。

**LA 层为什么安全**：任何来源的字节都必须过同样四个条件。错的命中只花一次下载，不会变成一条错引用。

## 第四条纪律：**身份 ≠ 版本**

四个条件只证明"这是那篇论文"，不证明"这是期刊版"。`R141` 就是从 arXiv 取的，存成了期刊文章的名字。
`versions` 读 `PROVENANCE.jsonl`，给出 `preprint / accepted / vor / unknown`，每个标签都带 `version_basis`。
`vor` 若来自出版社域名一律写 **asserted**（没人拿它跟期刊 PDF 逐字对过）。要强制正式版：`--version-required vor`。

**年份不是装饰，是版本的守门人。** 2026-10-22 的实例：作者档案里的"Fan 1991"实际是
*Technical Report No. 157, June 1988, UC Berkeley*——**标题完全一致、作者一致、是另一份文件**。
正因标题会完全匹配，年份必须保留否决权：年份**缺失**只警告（老抽印本常不印年），年份**不同**直接拒。

**`unknown` 是正确答案时不要美化它**：本库抽查六份"看着像出版社 PDF"的文件，第 1 页既无 DOI、无卷页、无版权行——
它们是已发表论文的 arXiv 版。**版本只能在下载时记录，事后从字节里猜不出来。**

## 第五条纪律：**不猜、不删、不降阈值**

- 不匹配的文件**留在原地不动**（`--from-dir` 输出 `unmatched`），绝不猜一个名字塞进库里；
- 失败或错配的文件进 `_misfiled/` + `MANIFEST.jsonl`，**永不删除**；
- 为了凑数量降阈值 = 把"下到了别的东西"变成"引用错了东西"。

## 长批次

`-v` 每次尝试即时打印；`--budget` 每篇墙钟上限（**不含 resolve 的元数据查询**，所以会略超）；
`--log` **逐次追加** JSONL，跑批总结写到 `<log>.summary.json`——不覆盖日志（早期版本在成功结束时把日志覆盖掉了，
正好毁掉它存在的理由）；`--resume` 据此跳过已完成项；`--skip-existing` 跳过库里已有同名文件的条目。

## 第五条半纪律：**投稿前跑 audit，别看感觉**

`audit` 把「条目」和「文件」双向对齐，输出六种判定，而且**故意不对称**——
因为"检查没通过"其实是四件不同的事：

| 判定 | 含义 | 动作 |
|---|---|---|
| `verified` | 标题块相符 | 无 |
| `other-version` | 同标题、年份不同 | **按你手里的版本引用**，或去取正刊 |
| `mismatch` | 标题实打实不符 | 换文件 |
| `check-failed` | 标题在、抽取器不行（字距/双栏） | 人工看一眼 |
| `unverifiable` | 无文本层，或条目根本没标题 | 机器到此为止 |
| `not-a-pdf` / `missing` / `orphan` | 假 PDF / 有录无文件 / 有文件无录 | 隔离 / 待办 / 清库 |

本项目实测（131 条目 / 160 文件 / 5.6 秒）：`verified 67 · other-version 18 · mismatch 0 · check-failed 1 · unverifiable 4 · missing 41 · orphan 69`。
**`other-version 18` 是最该看的一行**：十八条引用会建立在预印本上，而书目指向期刊。

⚠️ 判定可以越调越松，直到什么都不报。所以测试里钉了一对**已知错配**（ORI 续作冒充 NRI），
它必须永远判成 `mismatch`——这也是为什么兜底用「压缩后前缀」而不是字符 n-gram（那对续作的 4-gram 重合度是 0.79）。

## 台账：数字由脚本生成

`--write` 只替换 `<!-- BEGIN GENERATED:reading-status -->` 与 `END` 之间的区域；散文（为什么建台账、完整性发现、开放项）在标记外，人写。
阅读状态（A 全文 / B 摘要+结论 / C 未读 / X 无效）**只查台账**，不要凭印象回答"读过了"。
`versions` 与 `ledger` 是两个正交维度：**读了没有**（A/B/C）与**读的是什么版本**（preprint/vor）。
引用期刊结论时，两个都要看。

## 第六条纪律：**先查清单，再下载；list 是唯一真源，综述是它的视图**（2026-10-22 涛哥定）

本项目的 R 编号注册表是 `literature/REFS.jsonl`（**唯一真源**），综述的「参考文献」节
是**从它渲染出来的视图**（`<!-- BEGIN GENERATED:refs -->` 与 `END` 之间）。两者由工具保证一致，
**手工改综述的参考文献节 = 下次 render 时被覆盖**。发现一篇新文献时的标准流程：

```bash
litfetch refs find "标题片段或 DOI"        # ① 先查清单——已在册就停（exit 1）
litfetch fetch refs.json --library literature/…   # ② 下载 + 四条件核验入库
litfetch refs add --title "…" --year 2026 --authors "Klimesch, J., et al." \
    --venue "*Nature* 657" --doi 10.1038/... --note "判定注释" --group "【组名】"
                                           # ③ 注册（查重失败仍会拒绝）+ 自动重渲染综述
litfetch refs check                        # ④ list ↔ 综述 一致性；投稿前跑
```

为什么必须是这个顺序：本项目 2026-10-22 的教训——**16 组重复、22 个多余编号**，
成因就是"不同批次各自编号新文献，没人回头查重"，其中一组（Rainforth）占了三个号。
`refs find` 命中时 exit 1，脚本化流程据此停下。

**不重编号**：编号跨文档共享（综述、CA-1…CA-7 报告、命题卡片），空号（R65–R89 及已合并的号）
一律不回收；新号从 max+1 起。渲染只负责 GENERATED 区内；区外的散文（编号说明等）人写人管。

## 第七条纪律：**整份清单先分类，再只下能下的**（2026-11-01 涛哥定）

拿到一批文献（几十上百条）时，别逐条盲试下载。`classify` 先**静态**给每条定性——不联网、
按域名/DOI 规则即可出结果——然后 `--fetch` 只处理 `AUTO` 那批，其余如实分流到人工/办公室。
输入是 litfetch 的 `refs.json`，或项目清单 CSV（`CID,link,title_hint` 三列）。

| 判定 | 含义 | 动作 |
|---|---|---|
| `AUTO` | 本机 curl 直下可达（arXiv / PMLR / CVF / ICLR / 项目页 / 机构库直链） | `--fetch` 自动下，同样过四条件核验 + 写 provenance |
| `OFFICE_SUB` | 订阅墙（Elsevier/Wiley/Nature/Springer/ACM/IEEE 的 DOI） | 换有机构订阅的机器，或文献传递 |
| `BROWSER` | **OA 但需浏览器**：PMC/PubMed 的 PDF 是 JS 门控；MDPI/Frontiers 是反爬 403 | 用真实浏览器打开 → `import`；**不是付费墙，别误报** |
| `REPO` | github 代码/数据集库，不是论文 PDF | `git clone` |
| `ALT_SOURCE` | 源 PDF 本身损坏（`--known-broken` 标出） | 找替代副本，重抓同一 URL 无用 |
| `UNKNOWN` | 无法归类 | 人工看一眼 |

```bash
litfetch classify pending.csv --known-broken C040 --plan plan.csv   # 出定性台账
litfetch classify pending.csv --fetch --library <库> --layer L0      # 只下 AUTO
```

**三条被真实失败逼出来的规则，已钉进 `io/reachability.py` 与 `tests/test_reachability.py`：**
- **PMLR 名带 `.html`**：落地页 `orujlu26a.html` 若直接拼 `.pdf` 会得 `orujlu26a.html.pdf`（404）。
  真实 PDF 在 `raw.githubusercontent.com/mlresearch/v{卷}/.../{id}/{id}.pdf`。
- **PMC 的"假 PDF"**：`/articles/PMC…/pdf/` 对 curl 返回 1 页 `Preparing to download …` 占位页，
  它是合法 PDF，只查 `%PDF` 魔数会被骗。`_valid_pdf_bytes` 对「1 页且含该字样」直接拒。
- **OA ≠ 本机可自动下**：MDPI 是开放获取，但沙箱直连 403；PMC 是开放获取，但 PDF 走 JS。
  它们是 `BROWSER`，不是 `OFFICE_SUB`——把反爬误报成订阅墙，会让人白跑一趟办公室。

**诚实的边界**：`AUTO` 只表示「本机可尝试」，其中一部分是落地页 scrape，
页内若没有 PDF 链接仍会失败；真正的成败由 `--fetch` 逐条给出（`AUTO-OK` / `AUTO-FAIL`），
不要在分类台账里就把「尝试」写成「已获得」。分类是静态的、可离线复现的；
下载是联网的、要核验的——两者分开，台账才不会撒谎。

## 第八条纪律：**订阅墙先查 OA 副本；"在校园网"救不了脚本**（2026-11-02 实测）

涛哥说「在校园网了」，实测结论与直觉相反，三条都要记住：

1. **脚本请求走沙箱固定代理出口**，不是校园网机构 IP（`cdn-cgi/trace` 显示 `loc=CN`，
   `--noproxy '*'` 直连根本不通）。所以**机构订阅对 curl 不生效**，"在校园网"不等于脚本有权限。
2. **403 大多是 Cloudflare `Just a moment…` 挑战**，不是订阅拒绝：PNAS / OUP / Wiley /
   Taylor&Francis / ScienceDirect / MDPI 全中。CF 判定的是 JS 执行，不是 TLS 指纹，
   也不是 IP 段——`curl_cffi` 伪装 Chrome、换代理、重试都不解决。
   `litfetch doctor` 能启 `--browser`（真实 Chrome CDP），但**临时 headless profile 不带
   你的机构登录会话**，实测两篇都超时。真正有效的是**你在自己已登录的浏览器里另存 PDF**，
   再 `litfetch import --from-dir` 批量核验入库——这是 L4 正规路径，不是权宜之计。
3. **订阅墙 DOI 不是死路**：OpenAlex / Unpaywall / Crossref 常登记合法的 OA 预印本（多为 arXiv）。
   2026-11-02 那轮 34 个订阅墙 DOI 里，**10 个有 arXiv 副本且 curl 直接下到真 PDF**。

```bash
litfetch classify pending.csv --oa-lookup --mailto you@campus.edu --plan plan.csv
litfetch classify pending.csv --oa-lookup --fetch --library <库> --layer L0
```

`--oa-lookup` 对 `OFFICE_SUB` 项查 OA 索引，命中即改判 `OA_COPY`（新增判定），
`--fetch` 会下它并**强制**用 DOI 的注册标题核验身份、写 `version=preprint`。
不加这个开关时 `classify` 仍是纯静态、可离线复现的——网络查询是显式选择。

**两条硬约束，别省**：
- **身份必须核**：OA 索引给的 arXiv ID 可能指向另一篇论文。用 Crossref/OpenAlex 的
  `declared_title` 对下载文件的首页标题块做核验；核不出来就 `OA-REFUSED`，绝不入库。
  那 10 篇是逐篇双源核对（Crossref + OpenAlex）后才入库的。
- **身份 ≠ 版本**：OA 副本是**预印本**，不是期刊正式版。provenance 写 `version=preprint`
  且 note 明示「NOT version of record」；引用期刊结论时不能拿它当正式版。

PMC 补充：落地页里能解析出**具名** PDF 直链（`.../pdf/nihms-1052082.pdf`），
但它和通用 `/pdf/` 一样是 JS 门控（返回 1.8 KB HTML，加 Referer/Cookie 都没用）；
Europe PMC 的 `fullTextPDF` 端点返回 404。PMC 整族按 `BROWSER` 处理，别再逐个试脚本。
