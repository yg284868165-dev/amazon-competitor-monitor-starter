# 项目接手说明

## 定位

这是一个无需 Amazon API 的美国站竞品监控器，通过 Playwright 驱动本机 Chrome，按产品项目采集商品详情、关键词排名和 Best Sellers 榜单，并生成 Excel 报告。

## 运行与验证

- App 启动器：双击项目根目录中的 `Amazon竞品监控.app`；App 依赖与项目根目录的相对位置，不可脱离项目单独移动；修改 `macos/AppLauncher.m`、图标或包信息后运行 `scripts/build_macos_app.sh` 重建并签名。
- 管理页面：`.venv/bin/python web.py`，地址 `http://127.0.0.1:8765`。
- 手动采集：`.venv/bin/python run.py all --project <project_id>`。
- 测试：`.venv/bin/python -m pytest -q`。
- 定时状态：`.venv/bin/python schedule.py status`；修改配置后需重新安装。
- 后台权限验证：`.venv/bin/python schedule.py permission-test`，不访问 Amazon、不发送微信。
- 微信日报/周报状态：`.venv/bin/python notify_schedule.py status`；发送逻辑入口是 `notify.py`。
- 版本备份：`main` 跟踪当前分享版远端；验证后用 `git push` 备份。

## 技术栈

Python 3.11、Flask、Playwright、BeautifulSoup、SQLite、openpyxl；前端是原生 HTML/CSS/JavaScript。macOS 11 使用 `config/settings.json` 指定的 Google Chrome。

## 目录与约定

- `config/*.xlsx` 是项目、竞品、关键词和 BSR 类目的配置真源；`project_id` 是关联键。
- `data/monitor.db` 是历史数据真源；`output/reports/` 是可重建产物。
- `app/collectors/` 负责采集，`app/parsers/` 负责解析，`app/comparison/` 负责变化判断，`app/reports/` 负责主报告和动态日报/周报 Excel。
- `app/notifications/` 负责日报、周报与 Server酱发送；SendKey 只从 macOS 钥匙串读取，禁止写入项目文件或日志。
- 概览页只保留各项目主报告；原重复的变化提醒工作簿不再生成。昨日日报和上周周报都通过下载接口从数据库按需生成带商品链接的 Excel。
- 竞品 ASIN 表格修改后必须进入前端未保存状态；切换标签页、继续批量添加或关闭/刷新页面前应提醒先保存，保存失败时不得离开当前页。
- 日报 Excel 将变化事件的 `event_time` 展示为“采集时间”；它只表示程序检测到变化的时刻，不得表述为竞品实际修改时间。
- 微信日报中的状态型字段排除日内回到原值的净零变化；价格、Coupon/Deal、企业价、库存、购物车卖家、跟卖、发货方、高退货率标签、商品页面状态和类目名称属于事件型字段，即使日终恢复也保留。评价数量不进日报。
- 每周一10:00的独立周报完整复盘上周每日实际进入日报的所有重点变化，并汇总评价变化，以及大小类目BSR、关键词自然位的完整自然周7/14/28日趋势；微信周报不受日报 `max_items` 上限限制，排名趋势不得进入日报。周状态使用“截至周末仍保持变化/周内已恢复原状态”，不得表述成系统已经判断业务上必须关注。
- 广告位禁止再按日中位数生成7/14/28日趋势。每次搜索的广告位、未观察到和采集失败均须保留；周报 Excel 展示近28天美国西部时间原始分时轨迹，微信只展示至少3个可比日、60%以上重复的中可信度，或至少4日、75%以上重复的高可信度观察，并将事实与策略推测明确分开。
- 周报按中国本机时间周一10:00发送，此时美国西部仍是周日傍晚；广告观察只能使用发送时已经完成的样本，不得把尚未发生的周日深夜档描述为已覆盖。周一14:00采集结束后下载的周报 Excel 才能补齐该美西自然周最后一个深夜档；如需微信周报也覆盖完整美西周，发送时间必须调整到周一15:00以后。
- Coupon/Deal/企业价从有值变为空时分别记录为活动取消；Deal 从可见促销模块识别 Prime Day、Prime Big Deal Days、Prime Exclusive、黑五、网一、秋促等活动标签，禁止用全页脚本文本判定活动；主图和图片集在同次采集中一起变化时只保留主图变化，单独副图变化仍保留图片集提醒。
- `BrowserManager` 当前每批创建未登录的非持久 Chrome 上下文；企业价只能采集该会话可见的前台内容。对 Amazon Business 登录后才显示的价格不得宣称已完整覆盖；需先实现专用持久登录配置。
- 商品快照保留 5至1 星 customer reviews 占比；rating 星级变化时，变化事件的 `details_json` 必须附带当前各星级占比，供日报和 Excel 显示。
- 商品详情需监控高退货率标签和大小类目名称；这些字段出现、消失或切换均进入日报并保留日内恢复过程。高退货率历史空值首次采集只建立基线，类目区域完全消失需连续两次确认。
- 商品页面连续经过全部配置重试仍命中 Amazon 404/Dogs 文案时记为“页面变狗”，命中明确 `no longer available` 文案时记为“商品下架”；两者及恢复均进入日报。普通 `Currently unavailable` 仍属于库存断货。
- `app/presentation.py` 统一商品身份与变化文案，`app/trends.py` 负责自然周7/14/28天绝对名次趋势，`app/candidates.py` 负责BSR新竞争对手筛选与人工状态。
- BSR新竞争对手不以“相较上批首次进榜”为前提：当前前100中未配置在监控ASIN列表、经确认为同类且上架天数小于项目设定值的ASIN应生成一次性提醒。
- 新品包含关键词按单词宽泛匹配：一个配置短语拆词后任一词命中商品文案即算命中，排除词仍优先；已有可匹配文案但未命中任何包含词时自动标记 `not_same`，只有未配置包含词、无可用文案或命中但缺少上架日期等无法完成判断的情况才保留 `pending`。
- 新品雷达允许人工填写或清空候选上架日期，保存后必须重算年龄与筛选状态；候选日期来源使用 `date_source=auto/manual` 区分，人工日期不得被普通候选补采覆盖。
- `config/new_product_rules.json` 是各项目新品同类关键词、排除关键词和最大上架天数的配置真源。
- `config/settings.json` 的 `retention` 节点控制自动清理：数据库历史、异常诊断和日志统一滚动保留30天；每次采集或重试前执行，固定Excel报告不删除。
- Git 只跟踪源码、测试、文档和配置；数据库、日志、诊断文件、生成报告、虚拟环境和缓存必须继续被 `.gitignore` 排除。
- 如所在网络的 GitHub HTTPS Git 通道不可用，可将 `origin` 改为 GitHub SSH 443 端口地址；macOS 11 上使用兼容的 GitHub CLI 2.76.2，不要盲目升级到要求 macOS 12 的版本。
- 不接入 Amazon API，不绕过验证码；遇到验证码或榜单不完整时保留诊断文件。
- Amazon 邮编初始化遇到瞬时导航中止时按 `settings.json` 的 `initialization_attempts` 重试，不能因单次 `ERR_ABORTED` 直接结束整批任务。
- 正式采集遇到 `ERR_FAILED` 等页面异常时，关闭异常标签页并按 `retry_delay_seconds` 退避后新建页面重试，不能持续复用 Chrome 错误页。
- 每个正常/重试批次必须在创建 `BrowserManager` 前把完整目标清单预写入 `collection_task_outcomes`；这样断网发生在首个页面或邮编初始化阶段时，手动及自动重试仍能恢复全部目标。历史无清单失败批次从当前启用配置按原任务类型重建。
- BSR 单页需先滚动并按 `bestseller.render_wait_seconds` 动态等待至少48条；整轮部分失败后按 `auto_retry.delay_seconds` 等待并自动精准重试一次，人工已处理时应跳过，禁止无限重试。
- 只有 LaunchAgent 以 `--scheduled` 启动的自动采集才发送异常通知；应等待自动重试结束后区分“已恢复/仍失败”。Server酱不可达时通知写入 `collection_alerts` 队列，由后续定时采集补发，不得因告警失败中断采集。
- 日报、周报和采集异常每次发送都应把当时生成的正文保存到 `notification_logs.body`，并按30天保留。管理页须允许查看历史正文和下载对应周期 Excel；失败的日报/周报可人工重发，后来已经成功补发的旧失败记录不可重复发送。采集异常继续使用 `collection_alerts` 自动补发，不走简报人工重发接口。
- 不手工修改子配置中的既有 `project_id`；项目改名应从管理页面级联 Excel 子配置、新品规则、数据库和指定项目的定时配置。

## 当前状态与下一步

当前支持多项目、批量录入、组合筛选、五页精简主报告、可识别商品名称、自然周7/14/28天排名趋势、日内运营事件、高退货率与类目变化、变狗/下架状态、带上架日期与同类判断的新品雷达、BSR动态渲染等待、失败项手动/单次延迟自动精准重试、30天核心历史自动清理、定时任务及提前唤醒；Server酱微信日报与周一10:00周报使用独立 LaunchAgent，SendKey 保存在 macOS 钥匙串。管理页面只绑定本机，无公网认证；迁移云服务器前必须增加登录认证并改用 Linux 调度器。
