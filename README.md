# 每日 X 阅读精选

每天北京时间 08:00，目标 10 条；科技、AI、股市、投资、读书、学生羊毛尽量均衡。邮件每条只放原帖链接、短中文介绍和一句推荐理由，不转载原帖全文。投资侧重价值投资和原理学习，羊毛需写清资格、地区和有效期，内容不足时宁缺毋滥。用户允许价值投资、投资方法和读书每期最多3条经典旧帖，须核实正文并标明原帖日期，不能冒充近期内容。筛选偏好及账号屏蔽名单见 [settings.json](settings.json)。

## 当前进度

脚本、邮件模板、发送记录和 GitHub Actions 工作流已写入。QQ 连接测试已投递且用户确认收到。更新密钥并缩短检索请求后，普通模型、搜索响应与完整预览已恢复；最新预览有投资经典1条、近期0条，尚不满足十条或六领域近期覆盖。[首期内容示范](https://github.com/newwestcity/-/actions/runs/39580533147)已被QQ服务器接受，发送历史缓存也已保存。每日计划默认开启，可显式停用。恢复工作先读 [项目进度记录](PROJECT_STATUS.md)。

已核对的 API 根地址是 `https://n.tokeness.dev/v1`，模型是该密钥目录实际列出的 `gpt-6-luna`。普通对话走 `/chat/completions`，日报搜索走 `/responses`；单次接口检查通过不等于完整日报已投递。

## 先验证 Tokeness

普通聊天调用与搜索能力分开验证。[API 密钥与普通调用检查](https://github.com/newwestcity/-/actions/workflows/check-chat.yml)使用已经配置的 `DIGEST_API_KEY`，默认只读取 `/models`，不调用模型。仅手动勾选 `chat_call` 后，才从目录中选择较小 GPT 型号并发送一次 `model + 单条 user 消息` 请求；这可能计费，不加搜索工具或其他模型参数。此密钥的 GPT 分组曾拒绝示例中的 GLM 型号，返回 `model_not_configured`。模型目录返回成功只证明该路径能认证；普通调用须收到非空且正常结束的回复才通过。它不验证 X 搜索、不发邮件、不启用日程。

`https://n.tokeness.dev/v1` 与 `gpt-6-luna` 的普通对话已[实测成功](https://github.com/newwestcity/-/actions/runs/39462398796)：HTTP 200、非空回复、`finish_reason=stop`。这把密钥的目录列出了 GPT 6 等型号，未列出先前预设的 GPT 4/5 mini 型号。

[X 搜索验证](https://github.com/newwestcity/-/actions/workflows/check-x-search.yml)独立测试一次标准 Responses `web_search`（最多一次工具调用，240 秒超时），只有完整搜索记录与独立 X 原帖引用同时存在才通过；正文链接仅列为未验证候选，不会让检查通过。同时仅显示邮件变量/授权码是否存在，不公开它们的值、不连接 SMTP。此验证不启用每日发送。

X 搜索已[实测成功](https://github.com/newwestcity/-/actions/runs/39464437212)：HTTP 200，完整搜索记录，2 条有效 X 原帖来源。[完整日报预览](https://github.com/newwestcity/-/actions/workflows/check-digest-preview.yml)进一步运行实际的六领域搜索、筛选、来源/时效/去重校验与 HTML 渲染，不连接 SMTP；HTML 可从工作流附件下载。

[早期完整预览](https://github.com/newwestcity/-/actions/runs/39469686167)取得了15条过期引用；后续又遇到401与搜索超时。用户更新密钥后[目录认证恢复](https://github.com/newwestcity/-/actions/runs/39576484544)。拆分检索与编辑后的[最新完整预览](https://github.com/newwestcity/-/actions/runs/39578665264)已成功：投资经典1条、近期0条，每条官方核实元数据均通过。检索响应可用仍不保证每天找到十条优质内容或覆盖所有领域。[公开正文读取检查](https://github.com/newwestcity/-/actions/workflows/check-post-access.yml)已核实3条已有公开链接的官方 oEmbed 正文；它不调用模型或发邮件。

此步骤在 GitHub 自己的执行机器上运行，与 Codex 开发环境的保存、发布分开。当前 Codex 执行机器仍无法连到 `n.tokeness.dev`，请求被网络代理拒绝；这不能判断 Tokeness 本身或密钥是否正常。

需要重新验证时，沿用 GitHub 仓库 Secret `DIGEST_API_KEY`，依次打开普通 API、X 搜索或完整日报预览工作流，点 **Run workflow**。这些调用可能计费；不会修改变量、发邮件或启用每日计划。看到聊天正文中的链接不能证明检索成功；没有返回独立来源时停止发信。

## 运行

使用 Python 3.12 或更新版本，Linux/macOS；只使用标准库，无额外依赖。GitHub Actions 使用 Linux。云任务已隔离，沿用现有检出目录，无需另建 Git worktree。

在仓库根目录执行：

```bash
python3 -m unittest discover -s tests -v
python3 -m x_digest --help
python3 -m x_digest --check --send
```

最后一条只检查配置是否存在，不联网、不产生调用费、不发信；缺少配置时退出码为 2。

服务接入验证后，先生成预览，再测试发信：

```bash
python3 -m x_digest --fetch
python3 -m x_digest --fetch --send
```

`--fetch` 按领域分别检索，每类一次请求、一次联网搜索，默认六类合计最多六次请求，同时最多两类执行；每次请求等待最多 120 秒，不自动重试。部分领域失败时仍筛选其他领域的有效来源，不补入未经核实的帖子。预览同样可能计费，保存在忽略目录 `.local/preview/`，不会记录为已发送。邮件只有搭配 `--send` 时发送；预览和发送分开运行时会分别进行完整检索。

检索阶段仅寻找独立来源引用，使用短文本请求、每次最多1500输出token，不同时生成摘要或要求JSON schema。取得近期原帖引用后，通过 X 官方公开 oEmbed 接口核对正文摘录、帖子ID和当前作者，再调用一次普通模型生成中文候选。允许经典时，旧引用也须先核实正文，另调用至多一次普通模型，仅挑选股市、投资、读书中仍适用的长期内容；旧行情、当年估值判断、时效新闻和优惠不得算经典。每次完整运行读取合计最多20条公开原帖、至多两次普通编辑请求，失败不自动重试。

只有核实内容才能进入补充编辑，不以生成的链接冒充来源；独立读取或筛选仍失败时停止发信。该读取接口无模型凭据，返回的是官方嵌入内容，不能保证长帖全文或链接此刻仍可访问。来源日期和筛选计数会保存在忽略目录，预览工作流可显示不代表入选的来源诊断。邮件逐条标明原帖北京时间日期与“近期”或“经典阅读”；只有经典入选时会明确说明近期检索没有合格帖子。

本地环境变量由受信的环境管理器提供；程序不会自动执行或读取 `.env` 文件。

| 名称 | 用途 |
| --- | --- |
| `DIGEST_API_BASE` | 已核实支持搜索的 HTTPS API 根地址，包含 `/v1`；不是官网地址的推测值 |
| `DIGEST_MODEL` | 服务商实际支持搜索的模型名称 |
| `DIGEST_API_KEY` | 该 API 的密钥，安全录入 |
| `SMTP_USERNAME` | 开通 SMTP 的完整 QQ 邮箱地址，也是发件人 |
| `SMTP_AUTH_CODE` | QQ 邮箱 SMTP 授权码，安全录入；不是登录密码 |
| `MAIL_TO` | 收件邮箱；不设置时发给 `SMTP_USERNAME`，即自发自收 |

## GitHub 定时运行

[工作流](.github/workflows/daily-digest.yml)包含北京时间 08:00 的每日计划和手动运行入口。当前实现已完成内容示范投递，每日计划默认开启；仓库Variable `DIGEST_ENABLED=false`可停用，`true`或未设置为开启。每次测试任务仅公开生效开关的布尔值，不输出变量原文。

准备接入时，在 GitHub 仓库的 **Settings → Secrets and variables → Actions** 配置：

- Secrets：`DIGEST_API_KEY`、`SMTP_AUTH_CODE`。
- Variables：`SMTP_USERNAME` 必填；`MAIL_TO` 可选，默认发给自己。`DIGEST_API_BASE`、`DIGEST_MODEL` 可选覆盖；工作流已内置成功调用的 Tokeness 地址与 `gpt-6-luna`。这不代表完整日报已验证通过。
- 开通 QQ 邮箱 SMTP 后，授权码只填入 Secrets。不要写入仓库、聊天或工作流正文。

[邮箱连接测试](https://github.com/newwestcity/-/actions/workflows/check-mail.yml)单独发送一封明确标注的纯文本测试邮件，不调用模型、不启用日报。日志只显示配置是否存在和固定状态，不显示邮箱地址或授权码。[配置后的实际测试](https://github.com/newwestcity/-/actions/runs/39528686309)已返回 `smtp_accepted=true`，用户也已确认收到。邮箱和公开正文检查现只允许手动执行，后续代码推送不会再次发送测试邮件。

也可使用 [配置助手](scripts/configure_github.py) 代填普通字段：先在可访问 GitHub API 的执行环境中配置 `gh` 登录和非敏感变量 `SMTP_USERNAME`，再运行：

```bash
python3 scripts/configure_github.py
python3 scripts/configure_github.py --apply
```

第一条仅离线预览，第二条在只读预检成功后代填邮箱等字段；已提供且已核实的 `DIGEST_API_BASE`、`DIGEST_MODEL` 会一并填写，未提供时保留远端值。助手不创建或改写定时开关：已有设置保留，未设置时沿用工作流默认开启。在受信的本地终端增加 `--prompt-secrets` 可隐藏输入两项密钥并经 stdin 提交；无隐藏输入能力时停止。助手不会将云环境的代理密钥或占位符复制到 GitHub。

将代码放入仓库默认分支，手动运行该分支的工作流；不勾选 `send_mail` 仅生成 `digest-preview` HTML，勾选后真实发信。默认开启时每日计划运行搜索与发信；显式停用后需要将 `DIGEST_ENABLED` 改为 `true`或删除该变量才会恢复。推送触发的每日工作流仅运行测试。日报任务限制在默认分支，保证预览与发信使用同一份缓存历史。

GitHub 的定时任务可能排队延迟，08:00 是触发目标，不保证准时送达。公开仓库长期无活动时，GitHub 也可能停用计划任务；私有仓库须留意其 Actions 配额和服务商 API 用量。

## 来源、去重与投递

- 原帖链接必须同时出现在检索服务返回的来源元数据中。单独出现在模型生成正文里的链接不会进入邮件；这项检查证明检索来源中存在该链接，不等于独立审计整篇帖文。
- 只接受 X/Twitter 的 HTTPS 原帖地址，使用原帖 ID 去重。近期限制最近 7 天，学生羊毛最多 72 小时；发布时间由 X 帖子 ID 推导。经典仅限独立核实后编辑的股市、投资、读书，每期最多3条，与近期共享作者和分类上限，优先选择近期。
- 使用分类、作者数量上限和屏蔽名单分散来源；内容语义、活动有效性及账号偏好仍依赖检索与编辑模型的判断，屏蔽名单可逐步补充。
- 搜索失败、缺少来源、没有合格帖子或缺少发信配置时停止，不发送空日报。
- 只有 SMTP 接受邮件后才记录已发日期与帖子，默认保留 45 天；同一天重复运行会跳过发信。SMTP 接受不保证最终进入收件箱，首次需实际确认。
- `.local/state.json` 是去重历史。Actions 通过缓存保留，缓存被清理或丢失时历史去重会失效。SMTP 已接受但记录保存失败时需先核对收件箱，避免直接重跑造成重复投递。
- 不自动重试计费搜索或 SMTP 投递。每次 API 请求最多允许 6 次内置工具调用；真实 API 费用由服务商结算，工具调用和输出 token 限制不等于金额上限。

## 云开发环境

开发环境用来编写和验证脚本，其发布不会自动启动每日任务，也不是 GitHub Actions 部署的前提。环境设置保存失败时，可以继续在已配置凭据的 GitHub 运行器上验证服务，无需反复处理 Codex 的发布页面。

云环境的 HTTPS 代理密钥可能是按目的地替换的占位符，不应拿它直接当 SMTP 授权码。QQ SMTP 的端到端测试应在具备真实 Secrets 和 TCP/TLS 访问的执行平台完成，例如配置后的 GitHub Actions。
