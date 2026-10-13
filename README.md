# AI Bridge

一个本地网页"会议室"：你、Claude Code、Codex 三方在同一个聊天里开会，两个 AI 各用你本机已经登录好的 CLI（`claude`、`codex`），看得到彼此说的话，可以互相 @、分工、一起改同一个项目。

![icon](roundtable_preview.png)

## 要什么

- Windows（主要在 Windows 11 上用；codex 沙箱那几条是 Windows 专属的处理，别的系统没测过）。
- Python 3.11+（只用标准库，不用装包）。
- [Claude Code](https://docs.claude.com/en/docs/claude-code) 的 `claude` 命令、[Codex CLI](https://github.com/openai/codex) 的 `codex` 命令，都已登录，在 PATH 里。
- 工作文件夹最好是 git 仓库：干活模式靠 git 记每一步改了什么、能撤回；不是 git 仓库也能用，只是撤不回。

## 怎么开

- 双击 `start.bat`（带黑窗口，关掉就停），或者双击 `open.pyw`（后台开，不弹窗，没开就开、开了就直接打开网页）。地址 http://127.0.0.1:8765 。
- 第一次打开会让你开一场会议：标题、公告、工作文件夹（两个 AI 在哪个文件夹里干活）。
- 会议记录、附件都存在本文件夹的 `data\`（不进 git）。

可选的环境变量：

| 变量 | 作用 | 默认 |
|---|---|---|
| `ROUNDTABLE_PORT` | 端口 | 8765 |
| `ROUNDTABLE_LOG_DIR` | 会议记录放哪 | 本文件夹的 `data\` |
| `ROUNDTABLE_DEFAULT_DIR` | 新会议默认的工作文件夹（没有当前会议可带时） | 你的用户目录 |
| `ROUNDTABLE_NO_BROWSER` | 设成 1 启动时不自动开浏览器 | 不设 |
| `ROUNDTABLE_CODEX` | 设成 `exec` 退回旧的每轮一个 `codex exec` | `appserver` |

测试：`python tests\test_rt.py` 等（每个文件单独跑；用临时目录和假的 AI，不碰真的会话和记录）。

## 详细说明

你、Claude Code、Codex 三方在一个网页里开会。分两种模式：讨论模式（默认，AI 只能看不能改）和干活模式（AI 能改项目文件，带存档点，可撤回）。
以会议为单位：一场会议 = 标题 + 公告 + claude 一个固定会话 + codex 一个固定会话 + 聊天记录 + 附件。

- 开：双击 `start.bat`，会自动打开 http://127.0.0.1:8765 。关掉黑窗就是关服务。（后台常驻可以用 `pythonw roundtable.py`，设环境变量 `ROUNDTABLE_NO_BROWSER=1` 就不弹浏览器。）
- 开会：第一次打开没有会议，会弹窗让你填标题、公告和工作文件夹。顶部"新会议"只是切到新会议，旧会议保持原样（不关）；"会议列表"按时间倒序，点一场就切过去接着聊（会话 id 沿用），切换不改任何一场的状态。只有当前会议的 AI 会被叫。当前会议有 AI 在跑或排队时不能切会议（409）。
- 归档：列表里每场有"归档 / 取消归档"，只是标记（`meeting.json` 的 `status` = `archived`），内容和 AI 会话都保留；归档的排到列表底部、灰显。老数据的 `status: closed` 读作已归档。
- 每场会议各自的模型和 effort：点顶部 claude / codex 胶囊，里面可改"模型"（下拉预置 + 可手填）和 effort（第一项"跟随全局"）。存在 `meeting.json` 的 `models`，空值 = 跟随全局。胶囊上显示实际生效值，如 `claude · opus · 默认`、`codex · gpt-6.1-sol · medium`。
  - 跟随全局时只读：claude 读 `~/.claude/settings.json` 的 `model`，effort 查 `modelSettings.<模型>.effortLevel`（没有显示"默认"）；codex 读 `~/.codex/config.toml` 顶层 `model`、`model_reasoning_effort`。读不到显示"默认"。会议室不改这两个文件。
  - 调用时有值才加参数：claude `--model <m>` / `--effort <e>`；codex 在 `turn/start` 里传 `model` / `effort`（没设就传 config.toml 的全局值）。可选 effort：claude 为 `--effort` 支持的 low / medium / high / xhigh / max；codex 取 config.toml 的 `enabled-reasoning-efforts`，没有就 low / medium / high。
  - 改了会插一条"claude 换成了 X · Y"的系统消息。中途换模型，下一轮会慢一点、费一点（页面会提示）。AI 在跑或排队时不许改（409）。
- 删除会议（两步）：列表每场有"删除"，二次确认后把会议目录移到 `data\trash\<目录名>`（重名加后缀）。当前会议、AI 在跑时不能删。列表底部"回收站"可"恢复"（移回 `meetings\`），或者点"清空回收站"一键全删（确认一次）；清空会清掉每场的 `refs/roundtable/<目录名>/*` 的存档点引用，之后那几步干活模式的改动就撤不回了。路径限制在 `meetings\` 和 `trash\` 之内，目录名以服务端列表为准。
  - 清空回收站时，每场会议里 AI 自己的会话也一起删（移进回收站时不删，还能恢复）：这场用过的全部会话 id = 当前的 `claude_session` / `codex_session` + `old_sessions`（摘要续接换下来的旧会话，从这版起才记）。
    - claude：删 `~/.claude/projects/*/<id>.jsonl` 和同名目录、`file-history/<id>`、`session-env/<id>`、`todos/<id>*.json`。
    - codex：经 App Server 发 `thread/delete`（连它的记录文件）；codex 正在跑别的就等那一轮跑完。
    - 别的会议（还在的、回收站里剩下的）也在用的会话 id 不删；id 只认字母数字和横杠，防止拼出别的路径。
    - 在后台删，删完在当前会议插一条系统消息：每个会话删了几项 / 本机没找到 / 没删成的原因。
    - 你在终端里 `claude --resume` 接着聊过、另存成新 id 的会话，会议室不知道，不会删。
- 用：不写 @ 就是 claude、codex 都回；写 `@claude` / `@codex` 只叫被点名的。AI 回复里 @ 对方会触发对方，@用户 不触发，只在页面上高亮。AI 之间来回不限轮数，聊到有一方 @用户 或不再 @ 对方为止。
- 调度三条规则（讨论、干活模式都一样）：
  1. **你发言触发的 AI 调用：并行**。一条消息叫了两个 AI 就同时启动，谁先完谁先发；两个这一轮互相看不到对方的回答（前言里告诉了它们，回完发现冲突再 @ 对方）。
  2. **AI 之间的对话：排队**。由 AI 的回复 @ 另一个 AI 触发的调用，进一个全局串行队列，一个跑完下一个才开始。排到时如果它该看的消息都已经看过（增量为空），就不白叫。
  3. **同一个 AI 永远串行**（它只有一个会话）：每个 AI 一把锁，同一个 AI 的调用无论来源都排队等它自己；已排上、还没开始的不重复排（它开始时才取增量，自然会带上新消息）。
  - 增量递送：每个 AI 的 last_seen 在它拿到锁、开始被叫时取快照，回复写入后才推进。
  - 状态条 / `/api/state` 的 `running` 是列表，两个 AI 同时在想会都显示。切模式、切会议、压缩、交接时有 AI 在跑就拒绝（409）。
- 固定会话：每个 AI 在一场会议里只有一个会话，第一次被叫到时才建（开会本身不花额度）。之后每次只递"自它上次以来别人说的新消息"，它自己的话不重复发。公告改了，下次叫它时会在前面带一句"公告已更新为：…"，只带一次。
- 公告：页面顶部一栏，点开展开，可编辑，存在这场会议的 `meeting.json` 里。
- 附件：输入栏回形针选图片/文件（单个 25MB 内），存在这场会议的 `files\`；Codex 会收到这次递给它的新消息里的图片（最多 5 张），Claude 用 Read 读路径。语音功能没做，iPhone 上用键盘自带的麦克风听写即可。
- 交给终端 / 收回：点顶部 claude 或 codex 的状态胶囊 -> "交给终端"。交出去后网页里不再叫它；@ 它的消息照常记录，并提示"现在在终端里，回来后会补看"。窗口里有一条一键复制的命令：
  - `cd "<工作文件夹>"; claude --resume <id>`
  - `cd "<工作文件夹>"; codex resume <id>`
  在终端里干完活，回网页点"收回"，下次叫它时会带上"你刚才在终端里干了一段活"的说明，并补上这期间会议室里的新消息。会话还没建（这场会议没叫过它）时按钮是灰的，先 @ 它一次。它正在跑或排着队时不能交，等说完。
- 压缩：顶部"压缩"按钮，或在输入框单独发 `/compact`（不当普通消息，不叫 AI）。只压 claude：`claude -p --resume <id>` 发 `/compact`，实测会写 compact_boundary 并保留记忆。codex 不手动压：它快装不下时会自己自动压缩（2026-10-13 实测，会话记录里出现 `compacted`，压完记忆还在），阈值可用 `-c model_auto_compact_token_limit=<字数>` 调。旧的"写摘要换新会话"代码（`compact_one` 的退化分支）还留着，按钮不再走它。
- codex 的模型列表读 `~/.codex/models_cache.json`（codex 自己缓存的清单，只列 visibility=list 的），effort 选项按当前模型取。"跟随全局"跟的是 `~/.codex/config.toml` 的 `model`，在 Codex 桌面应用里换模型会改掉它；想固定就在会议里点胶囊选死。在终端里的 AI 会被跳过。
- 电脑上 Enter 发送、Shift+Enter 换行；手机上 Enter 换行，点按钮发送。手机上顶部那排按钮收在 ☰ 菜单里。
- 手机（iPhone）：电脑和手机都装 Tailscale，在电脑上运行 `tailscale serve --bg 8765`，再用 `tailscale serve status` 看到的 https 地址在手机 Safari 打开，可"添加到主屏幕"。
- 存储：`data\`（可用 `ROUNDTABLE_LOG_DIR` 改）
  - `current.txt`：当前会议的目录名，启动时据此恢复。
  - `meetings\<YYYY-MM-DD_HHMM>_<标题>\`：`meeting.json`（标题、公告、两个会话 id、每个 AI 的 last_seen / announcement_seen / handed_off、状态 open/closed）、`log.md`（给人看）、`log.jsonl`（程序读，重启恢复用）、`files\`（附件）。
- 只读保证（讨论模式）：claude 每次带 `--allowedTools Read,Grep,Glob`；codex 每轮在 `turn/start` 里明着传 `sandboxPolicy: readOnly`（见下面"codex 怎么接"）。
  - codex 的 MCP 服务器逐个关掉（`-c mcp_servers.<名>.enabled=false`，名单每次从 `~/.codex/config.toml` 的 `[mcp_servers.*]` 读），再 `--disable apps / computer_use / browser_use / image_generation`。原因：`-c mcp_servers={}` 实测关不掉，而有的 MCP（比如 node_repl）能读写文件，不关就等于没有只读。

## codex 怎么接（App Server，2026-10-13 起）

- 会议室常驻一个 `codex app-server --listen stdio://` 子进程（第一次叫 codex 时起，挂了下次自动重起），在它的输入输出上收发 JSON-RPC。启动参数：`-c windows.sandbox="unelevated" -c notify=[]`，加上面那串关 MCP / 关功能的参数。
- 每轮：`thread/resume`（新会议第一次是 `thread/start`）→ `turn/start`（明着传 cwd、`approvalPolicy: never`、`sandboxPolicy`、模型、effort）→ 等 `turn/completed` → `thread/unsubscribe`。每轮都重新接、说完就放手，所以你在终端里 `codex resume <会话号>` 聊过的，下一轮也接得上；交给终端时会议室不占着会话。
  - 回复取这一轮 `phase: final_answer` 的 `agentMessage`；改了哪些文件取 `fileChange` 条目的路径（started 和 completed 都记）。
  - 接不上原会话（比如会话号不存在）直接报"接不上原会话"，不偷偷新开。服务端发来的审批请求一律拒绝，免得卡住。
  - 模型 / effort 在会话里设一次会一直留着，所以"跟随全局"时也把 config.toml 里的全局值明着传。
- 为什么是 unelevated 沙箱、为什么去掉 PATH 里的 WindowsApps（2026-10-13 实测，codex-cli 0.162.1）：
  - elevated 沙箱每次跑命令前都要给桌面版运行时里的 `node_repl.exe` 刷权限，桌面版开着就被占住（os error 32），命令一条都跑不了。换一份干净的 `CODEX_HOME` 也一样，跟配置无关。
  - unelevated 起不了商店版 PowerShell 7（`CreateProcessAsUserW` 拒绝），所以 app-server 子进程的 PATH 去掉 WindowsApps，codex 就退回系统自带的 Windows PowerShell。
  - 实测（桌面版开着）：讨论档读得了、写不进；干活档能写项目里、项目外写不进；后台重启后接得上原会话；接得上旧 `codex exec` 建的会话；图片传得进去。
  - 没实测：unelevated 下"关网络"拦得多严。
- 退路：环境变量 `ROUNDTABLE_CODEX=exec` 切回旧的每轮一个 `codex exec`（只能手动切）。旧路在桌面版开着时跑不了命令。

## 谁能用哪个接口（身份规则）

发言人只有三种身份：`你`（用户）、`claude`、`codex`，加会议室自己的 `系统`。**每个参与者只能用自己的身份发言，`你` 只能来自用户在网页上的操作。**

- **代表用户的接口，只认浏览器**：`/api/send`、`/api/upload`、`/api/announcement`、`/api/mode`、`/api/undo`、`/api/undo/preview`、`/api/meetings/new`、`/api/meetings/open`、`/api/meetings/archive`、`/api/meetings/delete`、`/api/trash/restore`、`/api/trash/purge`、`/api/models`、`/api/handoff`、`/api/compact`、`/api/wrapup`、`/api/stop`、`/api/board`。要求请求头 `Sec-Fetch-Site: same-origin`（浏览器自动带），或带 `X-RT-Token`（每次启动随机生成，服务端把它注入网页 HTML，前端每个请求自动带）；两者满足其一，否则 403，正文"这个接口只给网页上的用户用；外部程序请用 /api/post 并写明自己的身份"。
  - 诚实说明：这是防误用，不是防故意伪造。curl 想手写 `Sec-Fetch-Site`、或者先 GET 首页拿 token，都做得到。它拦的是"外部程序不小心用用户的身份发了言"。
  - 经 `tailscale serve` 反代后 `Sec-Fetch-Site` 是否还在没有实测确认，所以留了 token 兜底：头被剥掉也能用。
- **`/api/post`，给外部程序用**（桌面应用里的 Claude 主对话、终端里的 Claude / Codex 等）：JSON `{"speaker": "claude" | "codex", "text": "..."}`，`speaker` 必填，只允许 `claude` 或 `codex`；`你`、`系统`、空、别的名字一律 400。这类消息页面上显示就是普通的 claude / codex，内部带一个不显示的字段 `origin: "external"`（会议室自己叫起来的回复是 `room`）。
  - 递送：外部 claude 的话会递给会议室里的 claude（它不知道外面那个 claude 说了啥），外部 codex 同理；`origin=room` 的才按"不含它自己的话"排除。前言里也告诉了两个 AI "署名 claude / codex 的话有些是会议室外同名 AI 说的"。
  - 路由：按"AI 发言"算。正文里 `@claude` / `@codex` 都会叫会议室里的对应 AI（外部发言的 @ 不按"@自己不算"排除）；`@用户` 标 to_user，不叫 AI。不带 @ 的不叫任何人。
  - 示例：
    `curl -s -X POST http://127.0.0.1:8765/api/post -H "Content-Type: application/json" -d "{\"speaker\":\"claude\",\"text\":\"（测试）不带@的外部发言\"}"`
- 只读接口（`GET /api/state`、`/api/messages`、`/api/meetings`、`/api/announcement`、`/files/...`、首页）不限制。

## 讨论 / 干活模式

- 页面顶部"讨论模式 / 干活模式"按钮（手机在 ☰ 里），切到干活模式会弹确认框；干活模式下标题栏有橙色标记。模式存在每场会议的 `meeting.json`（`mode`），新会议默认讨论。AI 正在回复时不能切。
- 切换时插一条系统消息"切到干活模式 / 切回讨论模式"，递给两个 AI；下次叫它们时前面再带一段"模式变了。…"说明新规矩。
- 干活模式**完全放开**（用户 2026-10-13 定）：跟平时用桌面版 / 终端一样，能改文件、跑命令、联网、用 git、用技能。只是不加载 MCP（拖慢每一轮）。
  - claude：`claude -p --permission-mode bypassPermissions --strict-mcp-config --mcp-config {"mcpServers":{}} --settings {"disableAllHooks":true} --output-format stream-json --verbose`（新会话再加 `--session-id`，之后加 `--resume`）。实测能写 `.claude/` 下的文件、能跑 git。最终回复取 `type: result` 事件的 `result` 字段（`-p` 下 stream-json 必须带 `--verbose`）。
  - 两种模式都不跑钩子：会议室每轮都是一个新进程 `--resume`，开场钩子每轮都会把一大段说明重新塞进会话，越聊越慢、越贵。
  - codex：`thread/resume` 传 `sandbox: danger-full-access`，`turn/start` 传 `sandboxPolicy: dangerFullAccess`。MCP 照样全关。
  - 撤回只管得到编辑工具的改动（claude 的 Edit/Write、codex 的 apply_patch）；命令写的文件、删掉的文件、git 操作撤不回，步骤消息里会标"它还跑了 N 条命令，命令写的文件不在记录里"。
- 前言按模式给 AI 写规矩：干活模式下动手前先 @ 对方说清要改哪些文件，别同时改同一个文件；删文件、git commit / push / reset、装软件、动项目外的东西这类撤不回的事，先在会议里说清楚、用户没反对再做；能用编辑工具改的别用命令写；改完 @用户 汇报。

## 落盘

- 顶部"落盘"按钮（手机在 ☰ 里），点了弹确认框。claude 在终端里、正在回复或排队时按钮是灰的。
- 点了以后插一条系统消息（只递给 claude，codex 看不到），叫 claude 把这场会议定下的东西落盘：工作文件夹里有 `.claude/skills/wrapup/SKILL.md` 就照它做（里面要用户在终端敲的步骤，比如提醒 /compact，在会议室里省掉），没有就用通用版（见下面"工作文件夹"一节）；然后只提交这次落盘改到的文件并推送，做完 @用户 汇报改了哪些文件、提交号、推送成没成。
- 这一轮 claude 强制按干活模式（完整权限）跑，不管会议现在是什么模式；会议本身的模式不变，下一轮回到原模式。实现：`state["force_work"]`，`process_job` 一开头就取走这个标记（提前返回也不会留到下一轮），再把模式明着传给 `call_ai`。
- 没有能落盘的结论时它会直说，不硬写。

## 强行打断（停）

- 有 AI 在跑或排队时，状态行（"xx 正在想…"）右边按人出红色按钮："停 claude""停 codex"，两个都在忙时再多一个"全停"。点了弹确认框。`/api/stop`，只给网页用；body `{"ai": "claude" | "codex"}`，不填 = 全停。
- 效果：被停的 AI 连同它起的命令（比如 git push）一起杀掉（claude 杀 `claude -p` 进程树；codex 杀整个 app-server 进程树，下一轮自动重起、会话从记录里接上）；它排着的全部作废；另一个 AI 照常跑，压缩不受影响。会议里插一条系统消息，两个 AI 下一轮都能看到。
- 实现：`state["epoch"]` 是每个 AI 一个计数，停了谁就给谁加一；排队时记下 `job_tag(ai)`（会议 id + 这个 AI 的计数），开跑时对不上就放弃；被打断的 AI 记在 `state["stopped"]`，收尾时报"被你打断了"。刚好在停的那一刻说完的话照登，但里面的 @ 不再叫人。
- 页面靠 `/api/state` 里的 `stop_each` 判断后端认不认按人停；旧后端只给一个"停"（= 全停）。
- 已经改了的文件不会自动还原（干活模式下可以按步骤撤回）；命令做到一半的事（比如推送）可能只完成一部分。

## 粘贴图片、拖文件

- 输入框里直接 Ctrl+V：剪贴板里有图片（截图、复制的图片）或文件就当附件上传，名字是"粘贴_时分秒.png"；剪贴板同时有文字时，文字照常粘进输入框。
- 把文件拖到页面上任意位置也会上传（拖进来时底部输入区有虚线框）。

## 引用回复

- 电脑（鼠标）：在消息上右键。选中了这条里的一段文字就出"引用这段回复 / 复制 / 引用整条回复"，没选就只有"引用整条回复"。系统消息上右键是浏览器自己的菜单。
- 手机（触屏）：每条消息头上有"↩ 引用"。先长按选一段字再点它，引用的就是这段；没选就引用整条。
- 引用后输入框上方出一条"回复 xx：…"，× 取消。发出去的消息顶上带灰色引用块（只引一段时带引号），点它跳回原文并闪一下。
- 发送时带 `reply_to`（消息编号），只引一段时再带 `reply_part`（选中的文字）；后端只认会议里真有的、你或 AI 说的话（系统消息不能引用），存进这条消息的 `reply_to`：整条就摘原文前 200 字（去掉 markdown 符号），一段就存那段（也截到 200 字），带 `part: true`。
- 递给 AI 的格式：整条是 `[你] （回复 [claude] 的“…”）正文`，一段是 `[你] （回复 [claude] 那条里的这段：“…”）正文`。
- 路由：没 @ 人、引用的是某个 AI 的话，就只叫那个 AI（等于回它）；@ 了人就按 @ 走；引用你自己的话又没 @，照旧两个都叫。

## 工作文件夹（不只这个项目能用）

- 开新会议时填"工作文件夹"（默认带上当前会议的，下拉里有用过的；一场会议都没有时用 `ROUNDTABLE_DEFAULT_DIR`，没设就是用户目录）。两个 AI 都在那个文件夹里跑：claude 进程的工作目录、codex 会话的 cwd、干活模式的 git 存档 / 撤回、"交给终端"的 cd，都跟着它走。存在 `meeting.json` 的 `workdir`。
- 开会后不能改：claude 的会话按文件夹存，中途换了 `--resume` 接不上。换文件夹就开新会议。
- 会议记录、附件存在会议室自己的 `data\`。工作文件夹里不含 `data\` 时，claude 会多带 `--add-dir` 指向它，好读附件。
- 前言只写通用说法 + 工作目录路径；各项目自己的规矩由 AI 自己从那个文件夹的 CLAUDE.md / AGENTS.md 读。
- 不是 git 仓库：讨论模式照常；干活模式照常改，但每步改了什么记不下来、撤不回（每场会议提醒一次）。
- git 仓库：存档 / 撤回照常，私有 index 每个仓库一份（`data\snapshot-<哈希>.index`）。
- 落盘：工作文件夹里有 `.claude/skills/wrapup/SKILL.md` 就照那份技能走；没有就用通用版（照那个文件夹的规矩写进合适的文档，没有合适的就新建 .md 纪要；是 git 仓库就提交，有远端就推）。
- 已知限制：讨论模式下 codex 在只读沙箱里，读不了只给你本人账户开权限的文件夹（实测 `AppData\Local\Temp` 下的读不了，`D:\` 下普通文件夹没问题；桌面、文档这类用户目录可能也一样）。干活模式不受影响。
- 页面：电脑边栏标题下面显示 📁 完整路径；窄屏 / 手机在标题后面带文件夹名（只在不是默认文件夹时）。

## 看板（公告 + 待办）

- 每场会议一块看板，存在 `meeting.json` 的 `board` 里（`board_seq` 是编号计数）。条目两种：公告（📌）、待办（可勾），每条记谁写的。最多 100 条，每条 500 字以内。
- 你：在页面上直接加、勾、删（`/api/board`，只给网页用；`{op: add|done|undone|del, kind: todo|note, text, id}`）。
- 两个 AI：在回复里单独起一行写 `【公告】…`、`【待办】…`、`【完成】编号`、`【删】编号`（方括号 `[待办]` 也认）。这几行会被收走、不显示在聊天里；代码块里的不算。外部经 `/api/post` 发的也认。
- 每次改动插一条系统消息"看板：xx 加了待办 #3：…"，另一个 AI 下一轮会看到（改的那个自己不重复收）。AI 第一次被叫、压缩后续接时会带上整块看板。这套写法写在会议室规矩里，老会话下一轮会补收到。
- 电脑：左边栏公告下面常显。窄屏 / 手机：公告栏右边"看板 N 待办"按钮，点开展开。

## 上下文显示

- 两个 AI 的状态卡片多一段"上下文 41%"，到 80% 变红；鼠标停上去看具体数（如 412k / 1M）。
- 数据是上一轮回话结束时的：claude 取 stream-json 里最后一次调用的 `usage`（输入 + 缓存读 + 缓存写 + 输出，子助手的不算），窗口大小取 `result.modelUsage` 里主模型的 `contextWindow`；codex 取 App Server 的 `thread/tokenUsage/updated`（`last.totalTokens` / `modelContextWindow`）。
- 存在 `meeting.json` 的 `ctx` 里，重启不丢。还没回过话、或刚真压缩完时不显示，下一轮回话后再出现。

## 排队和时限

- 你发话不会被 AI 之间的串行队列挡住：某个 AI 已经排在队列里时，你一说话它就马上另开一个并行的；队列里那份轮到时发现该看的都看过了，会自己跳过。
- 时限：讨论模式 10 分钟，干活模式 30 分钟（提交、推送大文件这类活）。
- 规矩改了（比如干活模式放开）会补给已有会话：`mode_seen` 存的是"模式 + 规矩文本指纹"，对不上就在下一轮前面补一段"会议室规矩改了，以这一版为准"，只补一次。

## 页面布局

- 电脑（宽 ≥1000px）：左边栏放会议标题、模式标记、按钮、两个 AI 的状态卡片（名字 / 模型 / 状态各一行）、完整公告；右边是聊天，内容居中、最宽约 860px。
- 中等宽度（601–999px）：单栏，内容居中。
- 手机（≤600px）：按钮收进 ☰，打开是两列大按钮；AI 状态只显示名字和状态，模型名收起；@ 快捷按钮可横滑；发送、附件、撤回按钮加大。
- claude / codex 的消息按 markdown 显示（标题、粗斜体、列表、引用、表格、代码、http(s) 链接），先转义再套标签；你和系统的消息原样显示。
- 改动归属（按 AI 自己的操作记录，不再是"前后两张快照的差"）：只有这个 AI 自己写文件的操作里出现过的文件，才算它这一步的改动。别的会话、别的进程同期改的文件（比如另一个窗口同时在出图）一概不算，撤回也不碰。
  - claude：从 stream-json 里收 `tool_use` 为 Edit / Write / MultiEdit / NotebookEdit 的 `file_path`（NotebookEdit 是 `notebook_path`）。
  - codex：从 `--json` 里收 `item.started / item.completed` 里 `type: file_change` 条目的 `changes[]`（每项 `path` + `kind`：add / update / delete）。
  - 路径统一规范成相对仓库根的正斜杠路径；项目外的路径单独标"⚠ 改了项目外的文件"（撤不回）。超时、崩溃时已经读到的记录也会用上。
  - "改前"：干活模式叫 AI 前拍一张 git 快照（临时私有 index `data\snapshot-<哈希>.index`，不碰你的 index 和当前分支，`git status` 里看不到；首次约 4 秒，之后零点几秒）。并行时两个 AI 各拍各的。生成提交挂在 `refs/roundtable/<会议目录名>/<序号>`，防 gc。`.gitignore` 忽略的文件和会议室自己天天写的 `meetings\`、`current.txt`、`snapshot.index` 不在快照里。
  - "改后"：AI 跑完后，对记录里的每个文件读当前字节算 sha256 存进这一步，不再拍第二张整体快照。碰了但内容没变的文件不算。
- 改动清单：有改动就插一条系统消息"claude 改了这些文件：改/增/删 路径…"，递给另一个 AI（不递给改的人自己），页面上这条带"撤回这一步"按钮。并行时两个 AI 时间上重叠又改了同一个文件，后记录的那条会标"⚠ 和 xx 同时改了这个文件"。
- 撤回：点按钮先弹确认框，列出要动的文件（新增→删除、修改→还原成改前、撤不回的不动），确认后才执行（页面先调 `/api/undo/preview` 取清单，确认后调 `/api/undo`）。只动这个 AI 这一步记录里的文件：改前存在的从它自己的改前快照按字节写回（`git cat-file blob`），改前不存在的删掉（空目录一并清）。写回前先检查：记录里每个文件现在的 sha256 必须等于这一步记下的"改后"，不等（被另一个 AI、你或别的进程又改过）整步拒绝，一个文件都不动，并列出对不上的文件。撤回后插系统消息"已撤回 xx 的这一步"。有 AI 在跑时不能撤。
- 注意：**被 git 忽略的文件改了撤不回**（改前快照里没有它）：它出现在记录里时清单标"撤不回"，其余文件照常撤；会议室自己的 `meetings\` 等目录同理。旧版（整体快照差异）记下的步骤仍能撤，按旧办法核对。
- 存档点 ref 不会自动清理。手动清理：
  - 看有哪些：`git for-each-ref refs/roundtable`
  - 删一场会议的：`git for-each-ref --format="%(refname)" refs/roundtable/<会议目录名>/ | ForEach-Object { git update-ref -d $_ }`
  - 全删：`git for-each-ref --format="%(refname)" refs/roundtable | ForEach-Object { git update-ref -d $_ }`，再 `git gc --prune=now` 才真正回收空间。删了 ref 之后那几步就撤不回了。
