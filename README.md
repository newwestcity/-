# 每日 X 阅读精选

每天北京时间 08:00，目标 10 条；科技、AI、股市、投资、读书、学生羊毛尽量均衡，中英文原帖统一用中文说明。投资侧重价值投资和原理学习，羊毛需写清资格、地区和有效期，内容不足时宁缺毋滥。筛选偏好及账号屏蔽名单见 [settings.json](settings.json)。

## 当前进度

脚本、邮件模板、发送记录和 GitHub Actions 工作流已写入。本地测试使用模拟服务，不代表 Tokeness 搜索、QQ 发信或云端日程已经验证。

用户提供的 Tokeness 调用示例为 `https://n.tokeness.dev/v1/chat/completions`，模型为 `glm-5.3-flashx`。这是 Chat Completions 对话示例，尚未实测；它没有证明第三方平台支持内置搜索或标准 Responses 搜索接口。现有日报搜索适配器是**有条件可用的标准 Responses 搜索接口**，不能直接认定它兼容上述 GLM 对话示例。如果 Tokeness 只提供文本模型，还需接入能够取得真实 X 原帖的独立来源。

## 先验证 Tokeness

普通聊天调用与搜索能力分开验证。[普通 API 验证](https://github.com/newwestcity/-/actions/workflows/check-chat.yml)使用已经配置的 `DIGEST_API_KEY`，先读取 `/models`，优先从固定候选中选择目录里实际列出的较小 GPT 型号，再发送一次 `model + 单条 user 消息` 请求，不加搜索工具或其他模型参数。此密钥的 GPT 分组曾拒绝示例中的 GLM 型号，返回 `model_not_configured`。新增或修改该验证工作流时会触发一次，也可手动执行；这会调用模型，可能计费。只有收到非空且正常结束的模型回复才判定成功。它不验证 X 搜索、不发邮件、不启用日程。

`https://n.tokeness.dev/v1` 与 `gpt-6-luna` 的普通对话已[实测成功](https://github.com/newwestcity/-/actions/runs/39462398796)：HTTP 200、非空回复、`finish_reason=stop`。这把密钥的目录列出了 GPT 6 等型号，未列出先前预设的 GPT 4/5 mini 型号。

[X 搜索验证](https://github.com/newwestcity/-/actions/workflows/check-x-search.yml)独立测试一次标准 Responses `web_search`（最多一次工具调用），只有完整搜索记录与独立 X 原帖引用同时存在才通过；只看到聊天正文中的链接不会通过。同时仅显示邮件变量/授权码是否存在，不公开它们的值、不连接 SMTP。此验证不启用每日发送。

此步骤在 GitHub 自己的执行机器上运行，与 Codex 开发环境的保存、发布分开。当前 Codex 执行机器仍无法连到 `n.tokeness.dev`，请求被网络代理拒绝；这不能判断 Tokeness 本身或密钥是否正常。

在 GitHub 仓库 **Settings → Secrets and variables → Actions → New repository secret** 中，将可用的 API 密钥安全保存为 `DIGEST_API_KEY`。再打开 [验证 Tokeness 接口](https://github.com/newwestcity/-/actions/workflows/check-connection.yml)，点 **Run workflow**：

1. 首先保持 `mode=api`。地址和模型已按用户的示例预填，无需填写 QQ 邮箱。只请求一次模型目录，不发邮件。
2. 连通成功后选择 `mode=search`，测试一次带内置搜索参数的请求，可能产生模型和检索调用费。该参数格式来自 GLM 官方示例；Tokeness 是否转发、指定模型是否支持，都由此次验证判断。
3. 运行摘要和 `tokeness-connection-report` 附件只保留脱敏状态、字段名及可能的 X 来源链接，不包含密钥、模型生成正文或原始错误正文。搜索返回的候选元数据仍需核实，不等于日报已经就绪。

这个验证工作流不会发送邮件、修改变量或启用每日计划。看到普通聊天内容不能确认搜索成功；如果没有返回检索来源，后续发信仍保持停止。

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

`--fetch` 每次会向配置的 API 发出一次请求，可能计费，包括仅生成预览时；不会自动重试。预览保存在忽略目录 `.local/preview/`。邮件只有搭配 `--send` 时发送。先确认预览，再测试发送会分别调用一次 API。

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

[工作流](.github/workflows/daily-digest.yml)包含北京时间 08:00 的每日计划和手动运行入口。定时发信默认关闭，需要先完成下述服务配置和验证。

准备接入时，在 GitHub 仓库的 **Settings → Secrets and variables → Actions** 配置：

- Secrets：`DIGEST_API_KEY`、`SMTP_AUTH_CODE`。
- Variables：`DIGEST_API_BASE`、`DIGEST_MODEL`、`SMTP_USERNAME`、`MAIL_TO`。
- 开通 QQ 邮箱 SMTP 后，授权码只填入 Secrets。不要写入仓库、聊天或工作流正文。

也可使用 [配置助手](scripts/configure_github.py) 代填普通字段：先在可访问 GitHub API 的执行环境中配置 `gh` 登录和非敏感变量 `SMTP_USERNAME`，再运行：

```bash
python3 scripts/configure_github.py
python3 scripts/configure_github.py --apply
```

第一条仅离线预览，第二条在只读预检成功后代填邮箱等字段；已提供且已核实的 `DIGEST_API_BASE`、`DIGEST_MODEL` 会一并填写，未提供时保留远端值。已有启用状态会保留，新配置的定时开关设为关闭。在受信的本地终端增加 `--prompt-secrets` 可隐藏输入两项密钥并经 stdin 提交；无隐藏输入能力时停止。助手不会将云环境的代理密钥或占位符复制到 GitHub。

将代码放入仓库默认分支，手动运行该分支的工作流；先不勾选 `send_mail` 检查生成的 `digest-preview` HTML 附件，再勾选测试发信。真实收信确认后，将仓库 Variable `DIGEST_ENABLED` 设为 `true`，每日计划才会运行搜索与发信。推送触发的任务仅运行测试。日报任务限制在默认分支，保证预览与发信使用同一份缓存历史。

GitHub 的定时任务可能排队延迟，08:00 是触发目标，不保证准时送达。公开仓库长期无活动时，GitHub 也可能停用计划任务；私有仓库须留意其 Actions 配额和服务商 API 用量。

## 来源、去重与投递

- 原帖链接必须同时出现在检索服务返回的来源元数据中。单独出现在模型生成正文里的链接不会进入邮件；这项检查证明检索来源中存在该链接，不等于独立审计整篇帖文。
- 只接受 X/Twitter 的 HTTPS 原帖地址，使用原帖 ID 去重。当前限制最近 7 天，学生羊毛最多 72 小时；发布时间由 X 帖子 ID 推导。
- 使用分类、作者数量上限和屏蔽名单分散来源；内容语义、活动有效性及账号偏好仍依赖检索与编辑模型的判断，屏蔽名单可逐步补充。
- 搜索失败、缺少来源、没有合格帖子或缺少发信配置时停止，不发送空日报。
- 只有 SMTP 接受邮件后才记录已发日期与帖子，默认保留 45 天；同一天重复运行会跳过发信。SMTP 接受不保证最终进入收件箱，首次需实际确认。
- `.local/state.json` 是去重历史。Actions 通过缓存保留，缓存被清理或丢失时历史去重会失效。SMTP 已接受但记录保存失败时需先核对收件箱，避免直接重跑造成重复投递。
- 不自动重试计费搜索或 SMTP 投递。每次 API 请求最多允许 6 次内置工具调用；真实 API 费用由服务商结算，工具调用和输出 token 限制不等于金额上限。

## 云开发环境

开发环境用来编写和验证脚本，其发布不会自动启动每日任务，也不是 GitHub Actions 部署的前提。环境设置保存失败时，可以继续在已配置凭据的 GitHub 运行器上验证服务，无需反复处理 Codex 的发布页面。

云环境的 HTTPS 代理密钥可能是按目的地替换的占位符，不应拿它直接当 SMTP 授权码。QQ SMTP 的端到端测试应在具备真实 Secrets 和 TCP/TLS 访问的执行平台完成，例如配置后的 GitHub Actions。
