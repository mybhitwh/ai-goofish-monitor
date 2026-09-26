# 设计：标题级预筛 + 跨任务判定复用

对应需求：`prd.md` 的 R1–R4；证据：`research/title-prefilter-evidence.md`。

## 1. 放置点（核心决策）

预筛放在 **`src/scraper.py::scrape_xianyu` 的列表循环内、去重判断之前、`random_sleep(2,4)` 与 `context.new_page()` 之前**。这是唯一能同时省下「详情页抓取」与「AI 调用」的位置；放在 dispatcher 里只能省 AI，详情页照样要开（每条还带 2–4s 延迟 + 25s 超时窗口 + 一次风控暴露）。

```
解析列表 API（basic_items，含 商品标题/商品链接/当前售价）
  ├─ 写 price_snapshots（全量，不受预筛影响）        ← 现状，不改
  └─ for item in basic_items:
       ├─ ① 预筛：标题命中排除词 → 写淘汰审计记录 + 计数 + continue   ← 新增
       ├─ ② debug_limit 上限                              ← 现状
       ├─ ③ 去重（processed_links / load_processed_link_keys） ← 现状（去重源加过滤，见 §4）
       ├─ ④ 开详情页 → 风控检查 → 组装 final_record        ← 现状
       └─ ⑤ dispatcher.submit(...)
```

判定逻辑做成**纯函数模块** `src/title_prefilter.py`，不依赖 Playwright / DB / 网络，可单测：

```python
@dataclass(frozen=True)
class PrefilterVerdict:
    matched: tuple[str, ...]      # 命中的排除词（原始写法，用于审计与日志）
    normalized_title: str         # 归一化后的标题（便于复盘）

def evaluate_title_exclusion(title: str, exclude_keywords: Sequence[str]) -> PrefilterVerdict: ...
```

## 2. 匹配语义（契约）

1. **归一化**：`unicodedata.normalize("NFKC", title)`（全角→半角）、连续空白压成一个空格、`casefold()`。依据：真实标题存在 `(25 6G WLAN版)`、`air 8  2026  m4` 这类断字与全角写法（见证据 §5）。
2. **两类关键词**：
   - 含 CJK 的关键词（`港版`、`换屏`、`iPhone Air` 的汉字部分等）→ 子串匹配。
   - 纯 ASCII 字母数字 token（`128G`、`M3`、`13寸` 中的数字段）→ 代码生成「数字边界」模式：数字前加 `(?<!\d)`，单位与数字之间允许空白（`128G` → `(?<!\d)128\s*g`）。**不使用 `\b`**：`ipadair8128g内存` 里 `g` 后接汉字，`\b` 不成立会漏判（证据 §5.2）。
3. **否定前缀保护**（必须）：命中位置左侧 4 个字符窗口内出现 `无|没|未|非|不|没有|无任何|全无` 时**不判命中**。依据：274 条标题里 85 条含「拆」而几乎全是 `无拆无修`；`进水/维修/换屏` 同理（证据 §5.1）。
4. 命中多条时全部记录，理由按 `、` 连接：`标题预筛命中：128G、Air7`。
5. 空关键词、纯空白、注释行（以 `#` 开头，可选）忽略；关键词总数上限 200（防误配置拖慢），超限在 API 层拒绝。
6. 判定是纯本地字符串运算：**不发网络请求**，不读 DB。

## 3. 数据模型与配置链路

新增任务字段 `title_exclude_keywords`（JSON 数组，元素为字符串），与既有 `keyword_rules` 完全同构：

| 层 | 文件 | 改动 |
| --- | --- | --- |
| schema | `src/infrastructure/persistence/sqlite_connection.py` | `tasks` 加列 `title_exclude_keywords_json TEXT NOT NULL DEFAULT '[]'`；新增迁移 `_migrate_tasks_title_exclude_keywords`（`app_metadata` 守卫键 `migration:tasks_title_exclude_keywords`，与 `_migrate_tasks_group_id` 同款） |
| 仓储 | `src/infrastructure/persistence/sqlite_task_repository.py` | 读：`payload["title_exclude_keywords"] = json.loads(payload.pop("title_exclude_keywords_json") or "[]")`；写：反向 `json.dumps`（照抄 `keyword_rules` 两行） |
| 领域 | `src/domain/models/task.py` | Task / TaskCreate / TaskUpdate / TaskGenerateRequest 加字段，复用现成的 `_normalize_keyword_values`（list 或按 `\n,` 切分的文本 → strip → 去空 → 去重） |
| API | `src/api/routes/tasks.py` | 透传；数量上限校验；其余校验不变（ai 模式仍要求 description，keyword 模式仍要求规则非空） |
| CLI | `spider_v2.py` | `--config` JSON 路径与 DB 路径都要把该字段透传给 scraper |
| 爬虫 | `src/scraper.py` | `scrape_xianyu(..., title_exclude_keywords: tuple[str, ...] = ())`，在 §1 的 ① 处调用 matcher |
| 前端 | `web-ui/src/types/task.d.ts`、`components/tasks/TaskForm.vue`、`api/tasks.ts` | 类型 + 多行文本框（与 keyword 规则输入框同款交互；ai 模式也显示）+ 提交/回填映射 |

迁移性质：`ALTER TABLE ... ADD COLUMN ... DEFAULT '[]'` 对已有生产库原地生效，幂等（守卫键）；不阻塞启动，无数据回填。

## 4. 淘汰记录的落库与去重语义

**落库**（审计 + R3）：

- 记录形态：`final_record`（含 商品信息 + 空 `卖家信息`）+ `ai_analysis = {analysis_source: "prefilter", is_recommended: False, reason: "标题预筛命中：<词>", keyword_hit_count: 0}`。
- 写入参数 `status="hidden"`：直接复用既有可见性机制——`_decorate_record_visibility` 对非 `active` 判 `_hidden_reason="manual"`，列表接口 `include_hidden` 默认 `False` 已把隐藏项排除，前端已有「显示已屏蔽结果」开关（`ResultsFilterBar.vue:188`）。于是 R3 的「默认不干扰 + 可复核」**零新增 UI**，卡片上直接可读命中词（reason 字段）。
- 不通知：`is_recommended=False`，`_notify_if_recommended` 自然不发。

**去重语义**（R2 的「不占去重位」）：

- `load_processed_link_keys` 增加过滤：`WHERE result_filename = ? AND COALESCE(analysis_source,'') <> 'prefilter'`。淘汰项因此不消耗去重记忆——规则修正或卖家改标题后，下一次运行会重新判定（最坏情况仍是一串零成本字符串比较，不会重新烧钱）。
- **必须同时修「淘汰 → 实际分析」的晋升路径**：`result_items` 现在是 `INSERT OR IGNORE`，若先写了淘汰审计行（`analysis_source='prefilter'`），后续同一商品真的被分析时会被 IGNORE 掉，结果永远停在「淘汰」。改法（新参数、保持既有语义）：

```sql
INSERT INTO result_items (...) VALUES (...)
ON CONFLICT(result_filename, link_unique_key) DO UPDATE SET
    is_recommended   = excluded.is_recommended,
    analysis_source  = excluded.analysis_source,
    keyword_hit_count= excluded.keyword_hit_count,
    raw_json         = excluded.raw_json,
    crawl_time       = excluded.crawl_time,
    status           = 'active'
WHERE result_items.analysis_source = 'prefilter'
```

- `WHERE result_items.analysis_source = 'prefilter'` 保证：非淘汰行仍是「首次分析为准」（现行为不变，用户手工隐藏/标注不会被覆盖）；只有淘汰行允许被真实分析替换。淘汰写入自身仍用 `INSERT OR IGNORE`（同一条每轮重复写不产生新行）。
- `price_snapshots` 不受影响：快照在循环之前对整页 basic_items 全量写入，行情样本口径不变。

## 5. R4：跨任务判定复用

**指纹**：`analysis_fingerprint = sha1(prompt_text + "\x00" + str(analyze_images) + "\x00" + decision_mode)[:12]`。criteria 或 base prompt 一改，指纹即失效，不会复用过时判据。

**落库**：`result_items` 增列 `analysis_fingerprint TEXT`（迁移同上）+ 索引 `idx_results_link_key ON result_items(link_unique_key)`（跨任务查询需要）。

**复用查询**（新增 `src/services/analysis_reuse_service.py`，纯 DB + 纯函数，便于单测）：

```
SELECT raw_json, price, crawl_time, analysis_source
FROM result_items
WHERE link_unique_key = ? AND analysis_fingerprint = ?
  AND analysis_source IN ('ai', 'ai-reused')
ORDER BY crawl_time DESC LIMIT 1
```

护栏（全部不满足才复用）：

1. 命中同指纹记录；
2. `crawl_time` 距今 ≤ `ANALYSIS_REUSE_TTL_DAYS`（默认 3 天）；
3. 价格可解析时 `abs(now - cached) / cached <= ANALYSIS_REUSE_PRICE_TOLERANCE`（默认 5%）。价格是「低价钓鱼」判据的输入，价格大幅变化必须重判。

**接入点**：dispatcher 的 `_build_analysis_result` 内、`_run_ai_analysis` 之前；复用能力通过构造函数注入（与 `seller_loader` / `saver` / `notifier` 同风格），dispatcher 不碰 DB：

```python
if job.decision_mode == "keyword": ...
if self._skip_ai_analysis: ...
reused = await self._reuse_lookup(job.link_unique_key, job.analysis_fingerprint, current_price)
if reused: return reused            # analysis_source = "ai-reused" + reused_from
return await self._run_ai_analysis(job, record)
```

- `ItemAnalysisJob` 增字段：`link_unique_key: str`、`analysis_fingerprint: str`（都在 scraper 侧算好）。
- 复用结果标记：`analysis_source="ai-reused"`、`reused_from={"crawl_time": ..., "task_name": ...}`、`reason` 尾部追加 `（复用 2026-09-25 21:09 的判定）`。**已定（2026-09-26）：复用项计入前端「AI 推荐」筛选**——读路径条件由 `analysis_source='ai'` 改为 `analysis_source IN ('ai','ai-reused')`（现锚点 `src/services/result_storage_service.py:59-62`，写入前先 grep 核对）。理由：该筛选表达的是"AI 判定为推荐"，与判定是本轮算的还是复用的无关；不计入会让复用出的推荐被静默过滤掉，与"分析失败被伪装成判定"属同一类"结论被吞掉"。需要区分来源时用 `analysis_source` 自身筛。
- 通知行为不变：`is_recommended=True` 就通知（各任务仍是独立监控）。

**已知取舍**（写在这里，避免后续反复讨论）：

- 复用不重算 `value_score` 相对本次价格参考的偏移；判定结论（型号/容量/国行/拆修/卖家画像）与价格无关，价格敏感的部分由护栏 3 兜住。
- 复用路径仍会抓一次卖家主页（现链路在 `_build_analysis_result` 之前就 `_load_seller_info`）。省掉这次浏览器开销需要把复用查询前移到 `_load_seller_info` 之前，并改用缓存记录里的 `卖家信息`——**列为 P2 可选项**，不在本次范围，先保证行为可解释。
- 不跨 `decision_mode`、不跨 `analyze_images` 取值复用（同一指纹条件已覆盖）。

## 6. 兼容性、发布与回滚

- **默认空规则**：字段缺省为空数组 → 未配置的任务与本任务上线前**逐字节等价**（唯一差别是 §4 的 upsert 与去重过滤，二者对非淘汰行是恒等变换）。因此可以先只给 1 个任务（建议「全国包邮·Air8写法」，样本 61 条、淘汰率 43%）灰度，再推广。
- **发布面**：后端代码随任务子进程自然生效；`src/api`、`domain`、`persistence` 变更需 `sudo systemctl restart goofish`（u12 NOPASSWD 已配）；`web-ui/` 改动必须 `cd web-ui && pnpm build`（AGENTS.md 约定）。
- **DB 备份**：首次在生产库上跑之前 `cp data/app.sqlite3 data/app.sqlite3.bak-2026-09-25`（`data/` 是运行时状态，禁止提交）。
- **回滚**：把任务的排除词清空即回到今天的行为；新增列与索引留库无害（也可 `git revert` 代码后不清理 DB）。淘汰审计行留在库里，默认隐藏、不干扰。
- **不碰三件套**：不动 `state/`、`.env`、任务调度；验证一律用 `--debug-limit` 调试跑，**不得为此启用当前处于风控冷却的任务组**。

## 7. 与其它任务/模块的边界

| 事项 | 归属 |
| --- | --- |
| 单次 AI 调用入参过重（图片不缩放） | `09-25-fix-ai-moderation-rejected-analysis`（本任务只减调用次数，不改入参） |
| 风控、串行化、重复调度 | `09-25-fix-risk-control-stop-loss` |
| 判定标准措辞（`prompts/*_criteria.txt`） | 用户手工维护，本任务不改 |
| 过期复核链路（`item_recheck_service`） | 不在范围（复核针对已分析商品的过期重检） |
| 关键词策略本身（是否收窄 `iPad Air`） | 用户侧任务配置决策，代码不参与 |
| `decision_mode=keyword` | 不受影响（该模式本来就不调 AI；预筛在其之上再省一次详情抓取） |

## 8. 测试与验证策略

- 单测（纯函数）：`tests/unit/test_title_prefilter.py` —— 归一化、粘连写法（`ipadair8128g内存`）、否定前缀（`无拆无修`/`没有进水`/`全新未拆封`）、多命中、空规则、大小写、上限。
- 单测（服务）：`tests/unit/test_analysis_reuse_service.py` —— 指纹命中/不命中、TTL 与价格护栏、无记录。
- 单测（dispatcher）：复用命中时假 analyzer 调用次数为 0；未命中为 1；`analysis_source` 与 `reused_from` 字段正确。
- 集成：任务 API 增删改查带 `title_exclude_keywords`（老 JSON 兼容）；`load_processed_link_keys` 排除淘汰行；落库 upsert 的「淘汰 → 分析」晋升。
- 离线评测：用 `result_items` 274 条真实标题跑规则集，断言命中 137 条且不含真目标（零误杀），脚本入库 `research/`（AC5）。
- 前端：`cd web-ui && pnpm build` 通过；任务表单新文本域的回填/提交手测（表单已有同款文本域的测试路径，不需要新增 E2E）。
- 回归口径：`.venv/bin/python -m pytest tests/ -s` 不劣于 130 passed / 3 failed / 3 skipped（3 个失败为存量）。