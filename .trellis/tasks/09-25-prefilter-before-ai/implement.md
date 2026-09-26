# 执行计划：标题级预筛 + 跨任务判定复用

对应 `prd.md`（R1–R4）与 `design.md`。**分两块独立交付**：A 块 = R1–R3（预筛，P0），B 块 = R4（复用，P1）；A 落地后可单独验收与提交，B 不阻塞 A。

前置阅读：`research/title-prefilter-evidence.md`、`.trellis/spec/guides/code-reuse-thinking-guide.md`、`.trellis/spec/guides/cross-layer-thinking-guide.md`。

## Step 0 前置准备

- [ ] 确认工作区干净（本任务开工前 `git status` 无未提交的无关改动）；确认 `.venv/bin/python` 可用。
- [ ] 备份生产库（**不提交**）：`cp data/app.sqlite3 data/app.sqlite3.bak-2026-09-25`。
- [ ] 记住红线：任务组「iPad Air 8」当前因风控事故处于禁用状态；本任务**不得**为了让预筛生效而启用它（见 `09-25-fix-risk-control-stop-loss`）。

## A 块：标题级预筛（R1–R3）

### Step 1 matcher 纯函数模块

- [ ] 新建 `src/title_prefilter.py`：`PrefilterVerdict` + `evaluate_title_exclusion(title, exclude_keywords)`。
      - 归一化：NFKC + 空白压缩 + casefold；CJK 子串 / ASCII token 数字边界（`(?<!\d)128\s*g`，不用 `\b`）；否定前缀保护（`无|没|未|非|不|没有|无任何|全无` 左 4 字符窗口）；多命中聚合；空词忽略；`#` 注释行忽略；上限 200。
      - 不引入第三方依赖，不读写 DB/网络。
- [ ] 新建 `tests/unit/test_title_prefilter.py`，用例直接取自真实标题（`result_items`）：
      - 命中：`ipadair8128g内存`、`ipadair8 128g`、`Air7`、`air7 m3芯片`、`M2`、`13英寸`、`iPhone Air`、`外版`、`港版`；
      - **不命中（AC3 否定前缀）**：`无拆无修`、`没拆没修`、`无进水`、`无维修`、`全新未拆封`、`非扩容机`；
      - **不命中（AC4 无正信号）**：`iPad Air 11英寸 紫色 WLAN版 256G，国行，在保`（不含 M4/Air8/2026 也必须放行）；
      - 边界：空标题、空规则、大小写（`128g` vs `128G`）、多命中、超限。
- [ ] 验证：`.venv/bin/python -m pytest tests/unit/test_title_prefilter.py -s`

### Step 2 离线评测（评审门 ①）

- [ ] 落脚本 `research/eval_prefilter_rules.py`：读 `data/app.sqlite3` 的 274 条标题，按拟定规则集输出「命中数 / 命中词分布 / 命中且被 AI 判推荐的条数 / 逐条人工核对清单」。
- [ ] 断言：命中 137 条、其中不含真正的 Air 8 / M4 / 256G 国行（AC5）；把结论追加进 `research/title-prefilter-evidence.md`（含最终规则集文本，供用户直接粘贴进任务表单）。
- [ ] **评审门 ①**：把最终规则集与命中清单给用户过一眼（这决定生产上会少看到什么），确认后再进入 Step 3。
- [ ] 验证：`.venv/bin/python .trellis/tasks/09-25-prefilter-before-ai/research/eval_prefilter_rules.py`

### Step 3 配置链路（DB → 领域 → API → CLI）

- [ ] `sqlite_connection.py`：schema 加 `title_exclude_keywords_json TEXT NOT NULL DEFAULT '[]'` + 迁移函数 + 守卫键（照抄 `_migrate_tasks_group_id`）。
- [ ] `sqlite_task_repository.py`：读/写 `json.loads` / `json.dumps`（照抄 `keyword_rules` 两处）。
- [ ] `src/domain/models/task.py`：四个 DTO 加 `title_exclude_keywords`，复用 `_normalize_keyword_values`。
- [ ] `src/api/routes/tasks.py`：透传 + 数量上限（>200 拒绝）。
- [ ] `spider_v2.py`：`--config` 与 DB 两条路径都带回该字段。
- [ ] 测试：`tests/unit/test_domain_task.py`（归一化/文本拆行）、`tests/integration/test_api_tasks.py`（增改查 + 老库缺省为空 + 超限拒绝）。
- [ ] 验证：`.venv/bin/python -m pytest tests/unit/test_domain_task.py tests/integration/test_api_tasks.py -s`

### Step 4 运行时短路 + 审计落库

- [ ] `src/scraper.py`：`scrape_xianyu` 增参 `title_exclude_keywords`；在 §1 位置（去重前、开详情页前）判定，命中则：日志 `[预筛] 淘汰 '<标题30字>' 命中 <词>`、写入本轮到 `prefilter_tally`、调用审计落库、`continue`；用本地集合避免同轮重复判定。
- [ ] `result_storage_service.py`：
      - `save_result_record` / `save_to_jsonl` 增 `status` 参数（默认 `"active"`，淘汰写 `"hidden"`）；
      - 分析写路径改成设计 §4 的 upsert（`WHERE result_items.analysis_source = 'prefilter'`），非淘汰行行为保持「首次为准」；
      - `load_processed_link_keys` 加 `COALESCE(analysis_source,'') <> 'prefilter'` 过滤。
- [ ] 任务收尾：打印本轮汇总（`[预筛] 本轮淘汰 N 条：128G×7 Air7×3 M3×2`），与 `processed_item_count` 并列（AC2/AC10 的证据来源）。
- [ ] 测试：`tests/unit/test_result_storage_service*.py`（upsert 晋升、快照不受影响、去重过滤）、集成用例（构造淘汰记录 → 再跑真实分析 → 行被替换成 `analysis_source='ai'`、`status='active'`）。
- [ ] 验证：`.venv/bin/python -m pytest tests/ -s`（全量；口径不劣于 130p/3f/3s）

### Step 5 前端

- [ ] `web-ui/src/types/task.d.ts`、`web-ui/src/api/tasks.ts`、`components/tasks/TaskForm.vue`：类型 + 多行文本域（ai 模式也显示，附一行说明「命中任一即跳过详情抓取与 AI 分析」）+ 提交/回填映射；i18n `zh-CN` / `en-US` 两侧同步加键。
- [ ] 结果页：**不新增 UI**——淘汰项以 `status='hidden'` 落库，默认被既有 `include_hidden=False` 排除，用户可用既有「显示已屏蔽结果」开关复核；卡片 reason 已带命中词。确认 `_hidden_reason` 显示文案对淘汰项不会误导（必要时在卡片上区分 `prefilter` 来源）。
- [ ] 验证：`cd web-ui && pnpm build`（AGENTS.md 要求改动即重建），并手测：任务表单存取该字段、结果页开关前后条数变化。

### Step 6 生产灰度（评审门 ②）

- [ ] 只给 `iPad Air M4 256G 全国包邮·Air8写法` 配规则（样本 61 条、淘汰率 43%），其余任务保持空。
- [ ] 校验 DB 升级：`.venv/bin/python -c "from src.infrastructure.persistence.sqlite_connection import init_schema, sqlite_connection; ..."`（或直接看服务启动日志无报错）；`curl 127.0.0.1:8000/api/tasks` 能读到新字段。
- [ ] 受控验证（**需用户先确认账号已过风控冷却**）：`.venv/bin/python spider_v2.py --task-name "iPad Air M4 256G 全国包邮·Air8写法" --debug-limit 3`，核对日志中 `[预筛]` 行与「获取详情」行数（AC2）。
- [ ] **评审门 ②**：向用户报告 `本轮淘汰 N 条 / AI 调用 M 次 / 对比改造前`（AC10），确认无误后提交。

## B 块：跨任务判定复用（R4，可延后）

- [ ] Step 7 指纹与落库：`result_items` 加 `analysis_fingerprint`（迁移）+ 索引 `idx_results_link_key`；`save_result_record` 写入指纹。
- [ ] Step 8 `src/services/analysis_reuse_service.py`：查询 + TTL/价格护栏（环境变量 `ANALYSIS_REUSE_TTL_DAYS=3`、`ANALYSIS_REUSE_PRICE_TOLERANCE=0.05`）。
- [ ] Step 9 dispatcher 接入：`ItemAnalysisJob` 加 `link_unique_key` / `analysis_fingerprint`；注入 `reuse_lookup`；命中返回 `analysis_source="ai-reused"` + `reused_from` + reason 追加复用时间；**「AI 推荐」筛选条件已定（2026-09-26）**：改为 `analysis_source IN ('ai','ai-reused')`（见 `prd.md` Decisions 与 `design.md`）。
- [ ] Step 10 测试 + 灰度：假 analyzer 计数断言复用为 0 次调用；TLL/价格护栏用例；灰度对比 4 个任务的调用次数（预期 274 → 约 79）。
- [ ] 验证：`.venv/bin/python -m pytest tests/ -s`

## 回滚点

| 时点 | 回滚方式 |
| --- | --- |
| Step 1–2（纯新增文件） | 删除文件即可，无副作用 |
| Step 3（迁移已跑） | 代码回滚后新列/索引留库无害；如需彻底清理，手工 `DROP COLUMN`（SQLite ≥3.35）或留待下次 |
| Step 4（upsert/去重改动） | `git revert` 对应提交；已写入的淘汰审计行默认隐藏，不干扰展示 |
| Step 6（生产已配规则） | 把任务排除词清空即恢复原行为（无需回滚代码） |
| B 块 | `ANALYSIS_REUSE_ENABLED=0` 关闭复用，或 revert 提交 |

## 验证命令清单

```bash
cd /home/myb/code/ai-goofish-monitor
.venv/bin/python -m pytest tests/unit/test_title_prefilter.py -s
.venv/bin/python .trellis/tasks/09-25-prefilter-before-ai/research/eval_prefilter_rules.py
.venv/bin/python -m pytest tests/ -s                 # 基线：130 passed / 3 failed / 3 skipped
cd web-ui && pnpm build && cd ..
curl -s 127.0.0.1:8000/api/tasks | .venv/bin/python -m json.tool | head -40
```

## 提交切分建议

1. `feat(prefilter): 标题级排除词匹配器与单元测试`
2. `feat(prefilter): 任务级排除词配置（DB 迁移/领域/API/CLI）`
3. `feat(prefilter): 详情抓取前短路、淘汰审计记录与去重语义`
4. `feat(web-ui): 任务表单支持标题排除词`
5. （B 块）`feat(analysis): 同商品跨任务判定复用（指纹 + 护栏）`