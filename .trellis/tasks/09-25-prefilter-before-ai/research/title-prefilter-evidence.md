# 证据：标题级可判定比例、误杀风险与跨任务重复分析

- 数据源：`data/app.sqlite3` 的 `result_items` 表（生产实例真实落库结果）。
- 采样范围：2026-09-25 21:09 那一轮扫描，任务组「iPad Air 8」（id=0）下 4 个任务，`crawl_time` 覆盖 `2026-09-25T21:09:33` ~ `21:09:53`。
- 4 个任务的 `ai_prompt_criteria_file` 均为 `prompts/ipad_air_m4_criteria.txt`，`analyze_images=1`，`decision_mode=ai`、`keyword_rules_json=[]`。
- 复现命令见文末。

## 1. 总量

| 指标 | 数值 |
| --- | --- |
| `result_items` 行数 | 274 |
| 唯一商品数（`link_unique_key` 去重） | 168 |
| AI 判定推荐（`is_recommended=1`） | 53 |
| `analysis_source` 取值 | 全部 `ai`（无 keyword / 无其它来源） |

按任务分解（`title` 级预筛判定见第 2 节）：

| 任务 | 分析条数 | 标题即可判定不符 | 占比 | AI 推荐 |
| --- | --- | --- | --- | --- |
| iPad Air M4 256G 全国包邮 | 77 | 36 | 47% | 17 |
| iPad Air M4 256G 上海包邮或自取 | 74 | 40 | 54% | 11 |
| iPad Air M4 256G 全国包邮·11寸写法 | 62 | 35 | 56% | 13 |
| iPad Air M4 256G 全国包邮·Air8写法 | 61 | 26 | 43% | 12 |

## 2. 仅凭标题即可判定「不符合硬性标准」的比例

判定规则＝`prompts/ipad_air_m4_criteria.txt` 里「一票否决」中**不依赖图片与卖家画像、只看标题即可定论**的子集：

| 规则 | 正则（示意） | 命中 |
| --- | --- | --- |
| 容量非 256G | `(?<!\d)(64|128|512)\s*g(b)?(?!\w)`、`1tb`、`2tb` | 86 |
| 非 M4 芯片 | `\bm[123]\b`、`m2/m3 芯片` | 31 |
| 旧代次型号 | `air\s*[567](?!\d)`、`第 5/6/7 代` | 24 |
| 外版 / 非国行 | `港版|美版|日版|韩版|欧版|外版|资源机|官换机|展示机|教育版` | 9 |
| 尺寸不符（非 11 寸） | `12[.,]9\s*寸`、`13\s*英寸?` | 7 |
| 非目标机型 | `iphone\s*air`、`ipad\s*pro`、`ipad\s*mini` | 5 |
| **合计（去重后命中条数）** | | **137 / 274 = 50%** |

- 这 137 条全部实际走了完整链路：搜到 → 开详情页 → 全量图片 base64 → 一次多模态 AI 请求。
- 其中 3 条（2 个唯一商品）AI 判成 `is_recommended=1`，但标题写明是 **iPad Air 7**（`自用ipadair7 11寸 256g…`、`【个人闲置 ipadair7 256G】2025年6月购入…`）——即预筛同时修掉这 2 个误报。
- 反向核对「误杀」：137 条里没有一条是真正的「Air 8 / M4 / 256G 国行」。逐条人工核对可疑项（标题同时含 `M4|Air8|2026` 与 `256G` 的 13 条）结论全部为真实不符：M2/M3 机型、`外版`、`iPhone Air`、`Air7 13英寸`。**在本样本上该规则集的误杀数为 0。**

## 3. 跨任务重复分析

`load_processed_link_keys` 只按 `result_filename`（即任务/关键词）取已处理键，因此同一商品被 N 个任务搜到时就会分析 N 次：

| 被几个任务分析过 | 商品数 |
| --- | --- |
| 1 | 100 |
| 2 | 37 |
| 3 | 24 |
| 4 | 7 |

- 冗余分析次数 = **106 次（占 274 次的 39%）**。
- 4 个任务当前共用同一份 criteria、同一 `analyze_images` 取值，理论上同一商品的判定结果应完全一致。
- 同一任务内无重复（`UNIQUE(result_filename, link_unique_key)` 生效）。

## 4. 预计收益

| 方案 | AI 调用次数 | 相对现状 |
| --- | --- | --- |
| 现状 | 274 | — |
| A：标题预筛（第 2 节规则集） | 137 | −50% |
| A + B：再跨任务复用唯一商品 | 79 | −71% |

A 额外省下 137 次**详情页抓取**（每次含 `random_sleep(2,4)`、新建 page、25s 超时等待）与对应的风控暴露面；A+B 不额外省详情抓取（复用仍要先拿到商品）。

## 5. 匹配器陷阱（实测，决定实现细节）

1. **否定前缀**：274 条标题里 **85 条含「拆」**，但绝大多数是 `无拆无修` / `没拆没修` / `无拆无修无暗病` / `全新未拆封`；含「进水 / 维修 / 换屏」的标题同样几乎都是 `没有进水`、`无维修`。若把 `拆修`、`进水` 直接作为排除词，会大面积误杀好商品。**匹配器必须带否定前缀保护**（`无|没|未|非|不|无任何|没有` 紧邻前置时不判命中）。
2. **无分隔写法**：真实标题存在 `ipadair8128g内存`、`ipadair8128内存质保`、`air 8  2026  m4`、`(25 6G WLAN版)` 这类粘连/断字写法。纯整词边界匹配会漏掉 `ipadair8128g`（`g` 后接汉字，`\b` 不成立），纯子串匹配又容易误命中。建议：容量 token 用「数字前不接数字」的宽松模式（`(?<!\d)128\s*g`），且对空格/全角空格/断字做归一化后再匹配。
3. **年份不可作为判据**：`2026` 出现在 `保修到2026年11月`（M3 机型）里；`2025款` 既可能是 Air7（M3）也可能被卖家写成生产/激活年份。判代次只认 `M1/M2/M3/M4`、`第 N 代`、`Air N` 这类芯片/代次标记。

## 6. 复现方式

```bash
cd /home/myb/code/ai-goofish-monitor && .venv/bin/python - <<'PY'
import sqlite3, re, collections
con = sqlite3.connect('data/app.sqlite3'); con.row_factory = sqlite3.Row
rows = [dict(r) for r in con.execute(
    'select task_name,title,price_display,is_recommended,link_unique_key from result_items')]
RULES = [
 ('容量非256G', r'(?i)(?<!\d)(64|128|512)\s*g(?:b)?(?!\w)|(?<!\d)1\s*tb|(?<!\d)2\s*tb'),
 ('旧代次型号', r'(?i)air\s*[567](?!\d)|air\s*第\s*[567]\s*代|(?<!\w)[567]\s*代'),
 ('非M4芯片',   r'(?i)\bm[123]\b|m2\s*芯片|m3\s*芯片'),
 ('尺寸不符',   r'(?i)12[.,]9\s*寸|13\s*英寸|13\s*寸'),
 ('非目标机型', r'(?i)iphone\s*air|ipad\s*pro|ipad\s*mini'),
 ('外版/非国行', r'(?i)港版|美版|日版|韩版|欧版|外版|资源机|官换机|展示机|教育版'),
]
hit = [r for r in rows if any(re.search(p, r['title'] or '') for _, p in RULES)]
print(len(rows), '条分析；', len(hit), '条标题即可判定不符；其中 AI 判推荐:',
      sum(1 for r in hit if r['is_recommended']))
byk = collections.defaultdict(set)
for r in rows: byk[r['link_unique_key']].add(r['task_name'])
print('唯一商品', len(byk), '；跨任务重复多余调用',
      sum(len(v)-1 for v in byk.values() if len(v) > 1))
PY
```

## 7. 未验证 / 存疑

- 上述「误杀为 0」是在 274 条真实样本上的结论，不是全域保证；上线前应保留审计记录与开关（见 design.md）。
- 未验证闲鱼搜索页是否存在容量/型号筛选器可直接收窄召回（无浏览器实测）；本方案只覆盖「列表解析之后」这一层。
- 未评估改写关键词（如把 `iPad Air` 收窄为 `iPad Air M4`）对召回的影响——这是用户侧策略，不在本任务代码范围。