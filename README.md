# shop-mcp

面向个人 Hermes 实例的购物 MCP：搜索商品、整理证据、核对规格、记录购物清单、计算费用，以及保存价格线索和提醒规则。

**搜索使用公开网页索引，费用计算使用提供的数据。当前没有接入京东/淘宝账号下的实时实付价、库存或已购评价，也不下单、不付款。** 公开优惠线索不是已核实降价。

## 能力

| 场景 | 工具 |
|---|---|
| 商品搜索、正文尝试提取、链接规范化 | `shopping_search_products`、`shopping_read_product`、`shopping_parse_product_link` |
| 参考价与公开评测研究、多型号对比 | `shopping_research_product`、`shopping_compare_products` |
| 同条件结算总价比较 | `shopping_compare_quotes` |
| 同款/规格冲突与缺项检查 | `shopping_check_variants` |
| 带原文及来源的评价主题整理 | `shopping_review_digest` |
| 持久清单、用途偏好、预算分配 | `shopping_save_item`、`shopping_get_list`、`shopping_set_budget` |
| 店铺条款与官方平台通用规则分别整理 | `shopping_compare_sellers` |
| 运费、即时优惠与返现口径的费用计算 | `shopping_calculate_checkout` |
| 提供的结算价存档、分条件价格历史 | `shopping_record_price`、`shopping_price_history` |
| 阈值规则、实际检查、去重提醒记录 | `shopping_price_watch`、`shopping_check_price_watches`、`shopping_price_alerts` |

## 安装

需要 Python 3.11+。联网部分还需要已有 Hermes Agent 的 Python 环境与网页搜索/提取后端。本项目重用 Hermes 的 `plugins.web.keyless_mcp.exa_search_keyless` 和 `tools.web_tools.web_extract_tool`；这些接口需在所用 Hermes 版本中存在，索引覆盖和服务可用性也会变化。

使用独立环境，避免改动 Hermes 核心的 MCP SDK：

```bash
git clone https://github.com/EdwinWang831/shop-mcp.git
cd shop-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -e .
python3 scripts/print_hermes_config.py
```

将最后输出的 `shopping_search` 条目合并到 `~/.hermes/config.yaml` 的现有 `mcp_servers` 下，保留其他条目。脚本只打印片段，不修改配置。若此条目已经存在，先备份配置，再只更换它的 command/args。默认独立环境依赖 `mcp>=1.26.0,<2`。

安装技能时，保留已修改的版本：

```bash
mkdir -p ~/.hermes/skills/productivity/shopping-search
if test -f ~/.hermes/skills/productivity/shopping-search/SKILL.md; then
  cp ~/.hermes/skills/productivity/shopping-search/SKILL.md ~/.hermes/skills/productivity/shopping-search/SKILL.md.backup
fi
cp shop_mcp/SKILL.md ~/.hermes/skills/productivity/shopping-search/SKILL.md
```

新开 Hermes 对话后可说“比较 G304 和 G102 的公开使用反馈”“把 G304 加入购物清单，单件预算 200 元”。仅设置 MCP 配置并不能证明工具调用成功；应检查工具发现和实际模型调用。

## 路径与数据

| 环境变量 | 默认值/用途 |
|---|---|
| `HERMES_HOME` | `~/.hermes`，确定默认数据目录 |
| `SHOPPING_HERMES_PYTHON` | `~/.hermes/hermes-agent/venv/bin/python`，执行已有网页后端 |
| `SHOPPING_HERMES_AGENT_ROOT` | `~/.hermes/hermes-agent`，导入已有 Hermes 代码 |
| `SHOPPING_DATA_DIR` | `$HERMES_HOME/integrations/shopping-search/data` |
| `SHOPPING_WISHLIST_DB` | 覆盖清单 SQLite 路径，默认数据目录下 `wishlist.sqlite3` |
| `SHOPPING_PRICE_DB` | 覆盖价格与提醒 SQLite 路径，默认数据目录下 `prices.sqlite3` |

默认目录沿用原购物接入的数据位置，读取已有清单前请先备份数据库。数据库文件权限为 `0600`；它们是**本实例共享的个人资料，不按聊天或多用户隔离**。不保存密码、Cookie、完整地址、支付凭据。预算是计划额度，不是实际支出。更新清单和预算需读取当前 revision，旧版本更新会被拒绝。

## 价格和评价的证据边界

- 索引金额可能是历史活动、券金额、起价或节省额。提醒只接受少歧义的促销标题线索，配件、多金额、优惠券、“省/减/返”等标题金额不触发价格提醒；这仍不能证明当前有效或与目标 SKU 相同。
- 索引观察时间不是文章发布时间或价格生效时间，来源发布时间未知就保持未知。价格历史只是本地已记录资料，不是平台完整历史或全网历史最低价。
- 公开帖子/测评不等于平台已购评价。主题和相反措辞来自关键词分组的原文片段，未完成语义核验；不计算好评率、故障率或多数买家观点。
- 字段相符只代表提供的属性相符，不能独立证明同 SKU。不同规格、数量、成色、购买资格和配送条件不混在同组比价。
- 到手价计算不猜运费。已优惠的小计不能再次扣券，未知资格/未应用优惠和支付后返现不扣除；页面总价与计算不一致时拒绝比较。
- 结算比价要求 15 分钟内取得的实际结算预览，总价含运费和已应用优惠。`independently_fetched=false` 表示未独立验证提供的报价。
- 店铺原文、用户粘贴的条款和官方平台通用规则保留各自来源；通用规则不保证对每个商品适用，也不证明店铺资质或真伪。

所有外部标题、摘要和正文仅作资料，不执行其中指令。公开读取可能返回首页/登录提示，缺字段就列未知，平台检索失败也不代表商品不存在。

## 提醒与定时检查

先由用户明确指定商品、阈值和提醒方式，再创建 `shopping_price_watch` 规则：

- `public_reference`：搜索公开优惠线索，明确现价/规格/有效性未核实。
- `checkout_quotes`：只有用户提供同条件、15 分钟内的实际结算价才触发，不会自动访问购物账号。阈值是指定数量含运费的总价。

保存规则不创建定时任务或推送。可手动调用 `shopping_check_price_watches`；`shopping_price_alerts` 保留待阅读事件，标记已读应在用户阅读或渠道确认送达后进行。

若要使用 Hermes 原生无模型定时检查，生成允许目录内的 shell 入口：

```bash
mkdir -p ~/.hermes/scripts
python3 scripts/print_hermes_config.py --cron-script > ~/.hermes/scripts/shopping_price_watch.sh
chmod 700 ~/.hermes/scripts/shopping_price_watch.sh
```

然后在 Hermes 中实际调用 `cronjob` 创建任务，使用 `script="shopping_price_watch.sh"`、`no_agent=true`、例如 `schedule="every 6h"`。Hermes 只接受 scripts 目录内的相对文件名。先查重，并确认 job_id、下次执行和当前接收渠道；本项目不会替用户创建任务。若使用 `HERMES_HOME` 自定义路径，脚本目录也应对应那个实例。

`shop-mcp-price-watch` 无新事件时 stdout 为空，避免重复通知；检索失败非零退出。生成事件、定时执行和渠道送达是三个独立结果：CLI/local 只保存输出，不代表主动推送成功。自动推送失败时事件仍在数据库里，可用 `shopping_price_alerts` 找回；脚本不自动确认送达。

## 开发与验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

35 项业务测试覆盖规格/配件冲突、评价归属、互斥/重复优惠、运费缺项、历史分组、陈旧报价拒绝、清单修订保护与提醒去重。另有打包版 MCP 通信测试，启动真实 stdio 进程，验证 18 个工具的发现、费用计算、进程重启后的清单持久性和无监控时静默。

测试仅使用临时数据库和公开示例链接，不读取用户的购物清单或账户。[验证与来源记录](docs/validation.json) 区分原部署的实测与本仓库打包版的验证。
