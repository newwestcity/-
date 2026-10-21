# 项目进度与恢复入口

更新：2026-10-21（北京时间）。此文件不含密钥、邮箱授权码或个人邮箱地址。

## 已确定的目标

- 每天北京时间08:00，目标10条X/Twitter原帖；中英文来源统一用中文摘要。
- 科技、AI、股市、投资、读书、大学生羊毛尽量均衡，宁缺毋滥。
- 股市和投资重在价值、经营、财务、估值、风险与学习，不要K线或喊单。
- 优惠必须仍有效，说明地区、资格、期限；排除诈骗和辱华攻击内容。
- 用户已明确允许股市、投资、读书每期最多3条经典旧帖，须标日期，不能冒充近期内容。科技和AI仍限7天，学生羊毛限72小时。
- 通过QQ SMTP发给自己；`MAIL_TO`可省略。用户已经确认收到连接测试邮件，不需要重复询问或配置SMTP。

## 已完成并验证

- Python标准库脚本、六领域搜索、中文编辑、来源/作者/日期检查、HTML邮件、45天去重与SMTP投递后记账。
- 经典必须经过官方oEmbed正文核实和独立编辑；原搜索摘要或模型自报日期不能获得经典豁免。
- 当前本地完整测试168项通过。没有真正选出合格帖子时停止，不发空日报。
- API根地址 `https://n.tokeness.dev/v1`；实际目录曾列出并已测试 `gpt-6-luna`。普通调用曾[成功](https://github.com/newwestcity/-/actions/runs/39462398796)，标准Responses搜索也曾[成功](https://github.com/newwestcity/-/actions/runs/39464437212)。
- 官方oEmbed[读取3条公开帖成功](https://github.com/newwestcity/-/actions/runs/39528636251)，只证明正文读取能力。
- QQ SMTP[连接测试成功](https://github.com/newwestcity/-/actions/runs/39528686309)，用户确认收件。
- GitHub默认分支包含实现；本地已有Git HTTPS认证，可正常读取和推送。不要提取代理凭据。

## 中断点和当前阻碍

1. 早期完整预览的15个引用全部超过7天，故没有入选；之后已按用户偏好实现经典最多3条。
2. 加入经典后的[完整预览 #5](https://github.com/newwestcity/-/actions/runs/39529399248)在请求阶段失败，没有产生日报。
3. 随后的[单领域诊断 #4](https://github.com/newwestcity/-/actions/runs/39529662246)返回 **HTTP 401**。这证明当次请求认证被拒绝，具体原因未取得；不能直接认定API余额不足。Codex自身额度中断与该接口错误应分别看待。
4. 恢复后的[目录认证检查 #7](https://github.com/newwestcity/-/actions/runs/39575507125)也曾返回HTTP401。用户随后安全更新了 `DIGEST_API_KEY`，新的[目录检查 #8](https://github.com/newwestcity/-/actions/runs/39576484544)已返回 **HTTP 200**、`model_directory_received=true`，实际可用型号仍为 `gpt-6-luna`；此检查未调用模型或SMTP。
5. 凭据更新后的[单领域生产搜索 #5](https://github.com/newwestcity/-/actions/runs/39576727332)等了240秒后超时，没有HTTP响应或已完成搜索证据。这次不是401，不能再把它当认证失败。
6. 检索阶段已改为只取独立工具引用、不生成摘要、不要求JSON schema、最多1500输出token；之后统一读取官方正文并编辑中文。[分离后的探针 #6](https://github.com/newwestcity/-/actions/runs/39578157821)中普通模型回复正常，短检索也HTTP200且工具完成，但12个来源都不是X原帖，不能当作已找到合格内容。
7. 经典类别的单次query已去除7天下限，避免“先检近期再检经典”超出每类1次工具预算。科技、AI和学生羊毛仍有日期限制。[完整预览 #6](https://github.com/newwestcity/-/actions/runs/39578665264)已成功：投资经典1条、近期0条、其余分类0条，`official_post_metadata_valid=true`，未发信。原帖 https://x.com/keyahayek/status/1812344441312813407 ，发布时间2024-07-14，不能声称10条或近期六领域覆盖。
8. 用户再次明确邮件只需原帖链接与简短中文介绍，不需转载原帖全文；当前模板已是简介和推荐理由。[首期内容示范投递](https://github.com/newwestcity/-/actions/runs/39580533147)成功：`smtp_accepted=true`，经典1条、近期0条，历史缓存保存成功；复用既有HTML，没有重复调用搜索或模型。邮件主题为“每日 X 精选｜2026-10-21｜1 条（首期预览）”。首次连接测试的收件已确认，内容示范的实际入箱尚待用户查看。
9. 一次性首期投递工作流已退役，避免再次触发。`daily-digest.yml`已默认开启08:00计划，显式 `DIGEST_ENABLED=false`仍停用。[实际开关验证 #33](https://github.com/newwestcity/-/actions/runs/39581021782)成功，公开`enabled=true; time=08:00 Asia/Shanghai`；CI测试步骤成功，push的digest job按设计跳过。cron为UTC00:00，即北京时间08:00。

## 下一步

1. 新密钥目录认证已经恢复，不要再次索取或重填密钥/SMTP配置。
2. 首期示范已发，不要再次投递相同内容；发送历史已供后续日报去重。
3. 每日计划生效开关已核实为true，首次自动计划为2026-10-22北京时间08:00。届时检查正常日程运行结果；搜索、编辑和GitHub排队会影响实际到信时间。手动同日发送与自动任务共用历史，避免重复。
4. 当前渠道只选出1条经典，尚无近期与六领域覆盖；此限制应明确说明。经典最多3条，不能放宽AI/科技/优惠时效来制造成功。每日运行若无合格内容继续停止空日报。

## 配置和运行注意

- GitHub Secrets：`DIGEST_API_KEY`、`SMTP_AUTH_CODE`；Variables：`SMTP_USERNAME`必填，`MAIL_TO`可选。已有绑定只检查存在性，不输出值。
- API目录检查默认只GET；手动勾选`chat_call`才调用模型。完整预览可能调用最多6次搜索和2次普通编辑；失败不自动重试。
- 邮箱连接与公开正文检查只允许手动执行，推送不会再发连接测试邮件。
- 本云执行机此前到Tokeness及GitHub API的请求被代理拒绝；Git原生读写与GitHub运行器可用。不要把云端连接受限误判为服务或凭据无效。
- 沿用GitHub Actions部署路线。此前Codex环境配置发布界面保存失败，不要再反复修改该配置或让用户重走发布流程。
- 不写入聊天中曾出现的密钥或授权码。进度记录和提交必须保持无凭据。
