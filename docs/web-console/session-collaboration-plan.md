# Web 会话协作实施计划

状态：正式功能与交办语义改造已实现，正在收口验证。原型不作为生产代码直接搬入。
本计划只覆盖同一 daemon 管理的项目与 Web UI，不新增 TUI 功能。

进度：P1–P4 已完成并验证（模型与持久化、受控执行闭环、Agent 工具与授权、服务契约与持久呈现）。
P5（正式 Web UI）已完成：输入框会话引用、对话下方协作提示、右栏协作面板与对方会话只读预览。
P6（收口）已完成真实 daemon 子进程和浏览器证据（模型受控）；真实默认模型仍未通过。

## 当前语义：可识别的协作与独立交办

本节覆盖后文历史实施记录中的“新轮次退役旧许可”和“停止只作用于来源”等旧规则。

| 身份 | 当前含义 |
| --- | --- |
| `collaboration_id` | 稳定协作组，当前沿用 `scope_id` 的持久身份；可显式复用给多个目标交办 |
| `assignment_id` | 一件独立交办，沿用 `request_id`；单项许可与控制版本独立 |
| `message_id` | 不可变消息身份；可信运行元数据与消息正文均可识别 |
| `turn_id` | 一次实际执行；审批恢复保持交办身份，并关联恢复后的执行 |

- 普通新用户轮次不会撤销既有协作。结果处理轮次携带原交办，不被解释成当前其他工作的插话。
- 指挥者可在 `contact_session` 指定已有 `collaboration_id`；被交办方不能隐式转派。
- `reply_to_collaboration(kind='progress')` 保存进度通知但不启动对方轮次；
  `clarification`、`supplement`、`reply` 仍在轮次边界处理，目标正在做的工作不被抢占。
- Web 分别展示交办状态、执行状态（含审批阻塞）、结果处理状态；不把轮次结束当作任务成功。
- Web 控制指定 `assignment_id`：仅接收抑制该项来源接续；撤销停止该项两侧关联执行，
  不影响同组其他交办或目标自己的工作。旧 scope-wide RPC 保留原有语义，不在新 UI 使用。
- 邮箱 schema 2 自动迁移 schema 1，保留历史、旧停止栅栏；未知版本仍拒绝。
- 回执在仅接收模式也自动刷新：有未完成交办/投递时每两秒查询，结束或切会话后自停。
  预览依赖稳定目标身份，避免重新渲染触发重复请求；繁忙显示可理解提示，不隐藏重试。

当前不做：普通用户消息持久排队、任务 DAG、审批代办、跨 daemon 通信、隐式多级转派。

### 本轮验证与收尾

- Python 协作、生产 Agent 图、daemon 子进程重启、会话与 wire 契约回归：297 passed。
- 前端全量：1198 passed / 1 failed；失败仍是既有 `themeContract` 的
  “flyouts settle in, and reduced motion is respected”，本轮未改主题。
- `tsc -b`、Ruff、契约生成 `--check`、前端构建、MkDocs、`git diff --check` 通过。
- `web/tests/collaborationUi.verify.ts`：真实宿主与 daemon、生产 UI、受控模型，
  引用发起→指挥者在对方处理期间执行独立工作→原交办回执仍触发接续→只读预览；
  1280/1024/820px 无横向溢出，无浏览器异常。
- 同脚本 `--receive-only`：不手动刷新、不启动来源接续，回执正文仍自动显示为待处理。
- 审批暂停不结束交办，恢复保留可信元数据并关联实际 turn；单项撤销不误停其他交办。
- 提交被拒不重试、不变成插话；输入框收到接受结果后才清稿，拒绝时草稿保留。

真实默认模型首次响应仍超时；受控模型验收不等同于外部模型成功闭环。
多会话汇总看板、直接编辑/补充交办的 Web 操作尚未实现；当前提供身份、消息、状态与单项控制基础。

优先级：**稳定性 → 职责内聚 → 有证据的扩展点 → 功能数量**。本计划由主 Agent 独立完成，不使用子 Agent。

## 1. 交付目标与明确不做的事

用户在 A 的输入框引用 B，要求 B 协助；A 组织必要上下文后继续独立工作；B 在自己的项目和会话上下文中处理；回复自动回到 A，在授权仍有效时接续。点击 B 可在现有右栏只读预览，正常闭环无需切换。

| 第一版必须交付 | 第一版不做 |
| --- | --- |
| 同项目、跨项目的既有会话；在已注册项目中新建目标会话 | 远程 daemon 互联、自动发现未注册项目 |
| 只读引用与发起请求分离 | 将选择引用等同于立即执行 |
| 新请求排队、明确回复、一次请求内的澄清与补充 | 群聊、永久上下文同步、多级自动转派 |
| 结果持久化、有限自动接续、停止后仅接收 | 通用工作流引擎、任务 DAG、插件调度器 |
| 页面关闭不影响后台协作；故障后的状态可核验 | 承诺任意工具副作用 exactly-once 或无条件自动恢复 |
| 原型确定的输入、执行记录、回执、预览触点 | 独立协作工作台、交付表单、角色切换、TUI 新界面 |

同项目会话目前共享目录。通信不会提供文件锁、独立 worktree 或自动合并；修改任务须明确分工，目标沿用自己的工作区、模型和审批约束。

## 2. 当前代码事实与必须尊重的边界

| 代码位置 | 已有行为 | 实施约束 |
| --- | --- | --- |
| `runtime/sessions/manager.py` | 项目级并发额度、单会话提交锁、生命周期锁、等待结算释放 | 协作执行仍走 `submit_ref`，不直接启动 graph |
| `runtime/sessions/runtime.py` | turn reservation、取消栅栏、结算持久化、审批暂停 | 运行结束事件不等于已完成结算；不能在锁内或结算回调中递归启动 |
| `runtime/service/commands.py` | `CommandReceipt` 表示实际启动；`command_id` 用于关联 | 不把既有 submit 改成“已排队”；关联 ID 不是现成幂等保证 |
| `runtime/steer.py` | 内存插话，下一模型步注入，结算时清空 | 不作为可靠的跨会话收件队列 |
| `runtime/daemon/application.py` | daemon 装配 router、各项目 manager 和持久化，连接独立 service | 协作协调器必须属于 daemon，不属于浏览器连接 |
| `runtime/service/recovery.py` | transcript 与实时 broker 不共享原子日志 | 不把 WebSocket 序号当作持久化提交凭证 |
| `sessions/transcript_projection.py` | 会话历史可由 checkpoint 重建 | 不能只往临时 UI 或可重建投影中追加回执 |
| `runtime/service/access.py`、`web_console/host.py` | ACL 与项目范围约束 | 源、目标都校验；内部工具调用不能绕过 scope |
| `web/src/components/composer/` | 富文本 pill，目前普通 mention 主要序列化为文本 | 会话引用新增类型化身份，不能仅靠显示名称或路径 |
| `web/src/stores/sessionViews.ts` | 既有多会话后台视图和恢复逻辑 | 预览复用有界订阅，不重建第二套 live reducer |

以上 Python 路径相对 `src/synapse/`。实现开始前须检查工作区是否有新的并行变更。

## 3. 最小内聚结构

新增一个 `src/synapse/runtime/collaboration/` 领域，不在 `local.py`、`manager.py` 或 Web store 中分散实现协作规则。

| 起步模块 | 唯一主要职责 |
| --- | --- |
| `model.py` | 不可变数据、合法状态转换、大小限制与接续判定 |
| `store.py` | 一个 SQLite 实现；事务、唯一约束、条件更新与有界查询 |
| `coordinator.py` | 请求、澄清、回复、撤销；保存后登记投递，校验请求归属和接续许可 |
| `runtime.py` | 有界投递循环；适配现有 router/manager、结算唤醒与重启核验 |
| `tools.py` | Agent 工具适配；从可信运行上下文取得来源，不让模型伪造来源或权限 |

这不是五个服务；它们在一个进程内，由 daemon 显式装配。只有需要替换为测试替身的执行和存储边界使用窄接口，不新增通用注册器、事件总线或抽象 repository 基类。

其他层只接线：

- `runtime/service/collaboration.py` 定义纯 DTO；`ports.py`、`access.py`、`contract_registry.py` 接入现有协议和授权。
- `app/agent.py` / `agent_assembly.py` 接收可选协作资源，装配工具；没有 daemon 协作资源时不启用，不强迫 CLI/TUI 引入后台服务。
- `daemon/application.py` 持有唯一协调器；关闭时先停止领取新投递，允许已执行轮次结算和写回，再关闭存储。
- `sessions/manager.py` / `runtime.py` 只增加必要的提交来源、启动关联和结算通知接点；不理解协作业务状态。
- Web 新增独立的协作投影模块；`useConsoleStore` 只组合动作，不成为调度器。

依赖方向为 Web/工具适配 → 协作协调器 → 存储与执行窄边界。纯模型和服务契约不得导入 UI、LangChain 或具体 daemon。

## 4. 数据与状态：区分消息、执行和接续

在 daemon 的 `state_dir` 下规划一个私有 `collaboration.sqlite`。由 daemon lease 保证实例 ownership；不向两个项目数据库做跨库原子事务，也不为每次请求单独建库。

起步只保留四类记录，具体表结构在 P1 固定：

| 记录 | 必要信息 |
| --- | --- |
| Request | 请求 ID、双方 `SessionRef`、来源工作代次、范围、状态、创建/更新时间 |
| Message | 稳定消息 ID、请求内顺序、发送方、类型、不可变正文及引用快照、展示锚点 |
| Delivery | 消息到指定会话的投递意图、执行标识、领取状态、关联 turn、错误/核验原因 |
| Continuation scope | 来源会话、工作代次、允许接续/仅接收/撤销状态、版本号 |

请求类型围绕一次委托，不把所有关系抽象成一张任意消息图。消息类型限制为请求、澄清、补充、最终回复；诊断信息单独记录，不自动触发往返。

三条状态轴不混用：

- 请求：待处理 → 处理中 → 待补充（可回到处理中）→ 已回复；异常为失败、撤销或待核验。
- 投递：待投递 → 已领取/准备启动 → 已关联执行 → 已结算；无法判定的启动结果进入待核验，不自动重新投递。
- 来源接续：有效 → 仅接收 / 已撤销；收到回复本身不能把撤销改回有效。

“已回复”只说明对方明确提交了最终回复，不代表来源已使用，更不代表代码验证成功。来源 UI 分别展示已送达、接续中和实际执行结果。

## 5. 第一版的确定性规则

### 5.1 投递不阻塞 Agent

1. A 的联系工具校验目标和范围，在同一事务中保存请求、首条消息和目标投递意图。
2. 工具立即返回“已接受请求”的凭证，不等待 B，不消耗 A 的并发额度干等。
3. 后台协调器按目标会话串行领取，通过既有 manager 提交；B 忙或待审批则保留排队。
4. B 沿用自身 graph 和设置，收到带来源的协作上下文。合作内容不冒充用户新授权。
5. B 通过明确回复工具保存结果，并在同一事务中登记回传。普通 final answer 不自动当作协作完成。
6. A 可继续独立工作，走到依赖点结束本轮；界面显示协作等待，不新增一个占用执行槽位的“等待轮次”。

同目标 FIFO；每个目标最多一个投递 worker；全局 worker 数量、队列容量、单条文本与返回页均有上限。达到上限明确拒绝新请求，不接收后丢弃。结算通知只唤醒协调器；领取和提交在锁外的统一运行循环中进行，辅以有界周期核验。

若 B 结束而既未回复也未提出澄清，显示“未返回协作结果”，不得伪造成功或无限自动追问。失败、取消和待审批分别保留明确原因。

### 5.2 首版仅在轮次结算后接续

第一版不把外部回执塞进现有 `SteerQueue`，不改造模型步级中断协议。

- A 忙：回执先持久化并显示；当前轮结算后再判断是否接续。
- A 空闲且许可有效：合并同一工作代次已到达的有限批次回复，启动一个接续轮。
- A 等待时不占用 manager semaphore；A、B 可以分别向前推进。
- 澄清/补充也在轮次边界处理，不保持双方执行锁互相等待。
- 工具说明明确禁止通过循环查询或 sleep 等待对方。

这是对原型“自动接续”的稳定实现，不承诺首版在任意工具批次后实时注入。只有实测证明轮次边界延迟影响使用，才独立规划持久化模型步收件能力。

### 5.3 工作代次与停止

工作代次不是新的用户任务系统，只是内部防止旧回执启动新工作的版本标记。

- 普通用户新提交默认建立新代次。自动接续和本次澄清继承原代次。
- 停止、改为仅接收、会话删除都会先撤销相应许可。
- 对活跃工作的新用户插话，首版保守使旧自动接续许可失效；请求仍存在，回执仍显示。不用关键词或模型猜测“是不是换了任务”。
- 用户明确要求继续某次回执时，建立新的有效许可；不静默复活旧代次。
- 关闭网页、切换项目、只读预览均不改变许可。

停止与接续必须由服务端在同一会话生命周期协调边界排序。提供一个“停止本次工作”的协作控制动作：撤销许可，撤销尚未启动的来源接续，并取消属于该代次的活跃轮次。前端不能用两个无关联 RPC 先 cancel 再禁用接续。

停止成功返回后，不允许任何该代次的新轮次启动。若接续已经先赢得启动权，停止必须识别并取消它，不能只拿旧 turn ID 调取消后忽略 mismatch。

停止 A 不自动终止 B：尚未投递的请求可撤销；B 已启动则允许回复，A 仅接收。绝不取消 B 正在执行的其他任务。

### 5.4 防止回复循环

接收方只允许对入站请求澄清或回复，不继承向第三个会话转派的权限。首版不允许 A→B→A 被误解释成新的委托链。澄清次数、自动接续次数有明确上限；达到上限保留消息并请求用户介入，不再自动唤醒。

## 6. 可靠性：不靠“重试一下”掩盖执行未知

**保证消息可恢复和投递幂等，不承诺任意外部工具恰好执行一次。**

- 请求/回复使用服务端绑定的稳定操作键（关联来源轮次及工具调用），唯一约束防止重试产生第二条请求；相同键但不同正文返回冲突。
- 保存消息与登记回传同库事务。数据库写入失败就返回失败，不显示“已发送”。
- 投递前持久化执行意图；将 `delivery_id` 和工作代次放入可信的冻结请求元数据，关联实际 turn。
- claim 使用版本条件更新；同一投递不能被两个 worker 同时启动。不把进程内锁当持久化幂等。
- 现有 `command_id` 只关联响应，现有 `on_started` 在执行提交后触发；两者都不能直接补上启动崩溃窗口。P2 必须明确准备、启动与关联持久化的线性化点。
- 进程在“可能已经启动，但关联记录未保存”处崩溃，重启后核对持久化请求标识/checkpoint；有确证则对齐状态，无法证明则进入待核验，不盲目再次执行。
- 过期领取不等于肯定没执行，不能直接释放重试。模型、shell 和文件副作用不能与 SQLite 事务原子提交。

| 故障位置 | 恢复策略 |
| --- | --- |
| 尚未写入成功 | 不返回接受凭证，调用方用相同操作键核验/重试 |
| 已写入、尚未领取 | 启动后继续调度 |
| 已领取、启动结果不明 | 核验执行标识；没有证据就暂停 |
| 已回复、来源尚未执行 | 从持久化投递恢复，重新检查代次和停止状态 |
| 回复已显示、浏览器断开 | 重连读取事实记录，不依靠旧 WebSocket 游标 |
| 项目不可用或会话已删除 | 明确标记原因，不自动注册项目或重新创建会话 |
| 待审批或执行失败 | 保留可见状态，不自动批准、不无限重试 |

顺序关闭先停止领取，再让 manager 结算并保存最后的回复，最后关协作库。异常重启不等于承诺自动重做中途被打断的代码修改。

## 7. 历史、实时展示与权限

### 单一事实来源

协作库是请求/回执事实的唯一来源；checkpoint 只记录送入 Agent 的协作上下文与稳定消息 ID。WebSocket 负责通知更新，不承担永久保存。

Web 对话按服务端返回的 `anchor_turn_id`、会话内顺序和 `message_id` 合并协作记录，不仅按浏览器时间戳排序。已送入 checkpoint 的回执与库中同 ID 回执折叠为一条。未接续的回执仍可读取，不需要制造一个空 turn 来持久化。

新增有界的协作查询：当前会话的待处理概览、指定请求详情、可见历史页关联的消息。实时事件只提示哪些记录/版本变化；重连、事件缺口和历史翻页重新查询。不得为此全量读取两个项目的 checkpoint 或不断加载全部历史。

首次实现即覆盖“完整历史由 checkpoint 重建后，来源、引用和去重标识仍正确”。不能只在实时路径加好看的卡片。

### 权限与来源

- 引用身份是明确的 `(project_id, thread_id)`；显示名称、文件路径和原始 prompt 都不作为授权依据。
- 选择引用是目标范围，不是立即运行。只读引用不产生协作投递；执行工具仍遵守用户指令、readonly 设置和已有审批规则。
- 入站用户轮次由授权层验证引用；工具从可信上下文取得被允许的目标集合，不允许模型填入来源身份、ACL 或工作代次。
- **已定：允许跨项目协作。** 目标可以位于同一 daemon 下已注册的另一个项目；跨项目读写分别验证双方能力，不因为同源就跳过目标侧校验。
- 浏览器侧项目范围不变：控制台仍不能直接寻址其他项目，跨项目只发生在服务端协作投递上，`project_scope` 继续约束 UI 的会话列表与直接读写。
- **已定：默认开启跨项目协作**，`project_scope=all` 与 `workspace` 都允许，无需额外配置。
- 仍提供显式关闭开关（按部署或按项目）：关闭后工具不向模型提供跨项目目标，投递一律拒绝，且错误不泄露其他项目的名称或路径。
- 需要留意的后果：`workspace` 原本是单项目边界（浏览器不能寻址其他项目）。跨项目默认开启后，A 项目会话可经服务端投递读写 B 项目会话，即边界从"不可达"变为"需用户显式要求协作"。面向多工作区/多用户的部署若依赖该边界，应显式关闭。
- 目标项目集合始终限制在同一 daemon 已注册的项目内；跨项目不获得目标项目的其他会话或文件访问权，也不因来源请求而放宽目标侧的审批与 readonly 设置。
- 目标项目集合由 daemon 配置决定并作为工具的可选范围下发；模型只能在该范围内选择，不能自行填入 `project_id`，也不能借内部工具绕过连接级约束。
- 跨项目只共享本次请求所需的背景与结论；来源不因此获得目标项目的其他会话或文件访问权。
- 排队到实际执行间再次核对目标存在、范围和授权有效性。无法重建有效授权时暂停，而非使用全权 daemon 身份替代。
- 非参与方无法凭请求 ID 读正文。隐藏项目不泄露标题或路径。
- 目标不会因为来源写了“忽略审批”而改变权限，也不能替用户批准操作。

文档结果优先返回有界结论；文件引用带所属项目、会话、相对路径及修订信息。读取走已有 artifact 服务的权限边界；不把另一个工作区的绝对路径当附件。超长正文明确截断或提供受控分页，不默认同步目录。

## 8. Web 接入范围

| 位置 | 改动方式 |
| --- | --- |
| `composerDocument.ts`、`mentionCatalog.ts`、输入动作清单 | 增加 session pill 和结构化引用；保留普通文本、文件/技能引用与图片行为 |
| `runtime-client` | 使用生成契约，增加有界协作查询与控制；不得手写另一套 wire DTO |
| 独立 `collaborationView` | 合并实时提示、持久消息和历史锚点；按消息 ID 去重 |
| `transcriptRows` 清单 | 紧凑联系记录和带来源回执，展开实际请求；不向 `Transcript.tsx` 塞协作分支 |
| 右栏清单及会话视图 | 临时只读会话预览；订阅有所有者和上限，关闭释放，不占用无限后台视图 |
| 底栏、输入停止按钮 | 区分正在执行与等待回执；等待时也可撤销接续，不依赖 active turn 存在 |

已有用户运行中发送仍是插话：新增的会话引用必须随 steer 一并验证和持久化，不能静默丢弃引用。前后端版本不支持时保留草稿并明确提示，不能退化成看似成功的纯文本。

新建目标会话沿用现有 session create，采用稳定 ref 处理重试；引用选择器取消不自动启动会话。预览不改当前执行对象，也不提供第二个输入框。

使用能力协商决定入口是否出现；没有能力时不显示可点击的空功能。不修改现有普通 submit 回执语义、不重写历史恢复协议、不引入路由或全局协作页面。

## 9. 分批实施与过关条件

下列阶段按依赖执行，每批都要求测试通过后再进入下一批；不是分别交付半成品给用户。最终仍以完整 Web 闭环验收。

| 阶段 | 产物与主要文件范围 | 进入下一阶段的门槛 |
| --- | --- | --- |
| P0 规划与边界 | 本文；固定交互范围、状态/恢复规则 | 已完成规划；未实现生产功能 |
| P1 纯模型与持久化 | `runtime/collaboration/model.py`、`store.py`；扁平 `tests/test_collaboration_model.py`、`test_collaboration_store.py` | SQLite 事务/幂等/容量/重开恢复、非法状态和参与方检查通过；不调用模型 |
| P2 受控执行闭环 | coordinator/runtime 适配；manager 必要接点；daemon 生命周期 | 两个临时项目、受控 turn runtime 完成请求→回复→接续；并发、停止竞争、崩溃窗口测试通过 |
| P3 Agent 与授权 | 工具、可信轮次上下文、结构化引用、跨项目目标范围与关闭开关、装配；明确回复/澄清 | 真实 agent graph + 假模型验证工具契约；不能伪造目标/来源、不能循环唤醒；跨项目默认可用，关闭后一律拒绝且不泄露目标信息 |
| P4 契约与持久呈现 | service DTO、registry、transport、访问控制、历史查询与事件；生成 manifest/TS | Python/TS fixture、旧客户端兼容、回执历史重建、恢复/去重、导入纯度通过 |
| P5 生产 Web UI | composer、独立协作投影、消息行、右栏预览、等待/停止 | 使用生产组件与模拟 runtime 的浏览器测试通过，不搬入原型 timer/场景数据 |
| P6 收口 | 真实 daemon 子进程恢复测试、文档和发布前检查 | 页面关闭/重开、daemon 重启、取消与迟到回复、多会话竞争全部有证据；仅此时标记功能完成 |

P1 的数据身份决定 P2 的启动关联。P2 与 P3 稳定后才冻结新增 wire 形状；不先让前端依赖易变接口。每批保持旧路径默认不变，未完成能力不向 UI 宣告可用。

### 首批具体工作

1. 固定 Request / Message / Delivery / ContinuationScope 的最小字段、允许转换和具名错误。
2. 为状态转换先写表驱动测试，特别是停止不可被回执反转。
3. 实现单 SQLite 存储、schema version、唯一约束、事务与有界查询。
4. 验证重复请求、不同正文冲突、同目标顺序、停止与回传并发、重开库后的未投递恢复。
5. P1 不修改正式输入框、不挂接真实 Agent、不开始后台投递。

### 必须覆盖的竞态与故障矩阵

- 同一请求工具重试 / 同一回复重试，只有一份消息和一个投递意图。
- 两个来源同时请求一个目标；目标已有普通工作或等待审批。
- 双向协作 A→B、B→A 不持有执行槽位互相等待；不会形成自动委托环。
- 停止与接续同时发生；多个浏览器发送或停止；旧工作不能在停止确认后重启。
- 一次来源工作有多份回复，合并有上限、逐消息去重，不并发启动两个来源轮次。
- 澄清答复、目标没有显式交付、执行失败、用户接管目标任务、会话删除、项目不可用。
- 故障注入到数据库提交前后、领取后/启动前、执行提交后/关联保存前、回执保存后/通知前。
- 只读引用不唤醒；未授权读取/发送被拒；跨项目默认可用但只能落在已注册且未被关闭的项目集合内，关闭后工具与投递都不能越界，拒绝信息不含目标名称或路径。
- live、断线重连、翻历史、checkpoint 重建四条展示路径一致。
- 中文 IME、键盘选择、窄屏、关闭预览释放订阅、无真实网络/模型的浏览器验收。

测试遵循窄测试 → 领域测试。wire 改动必须执行：

```powershell
uv run --no-sync python scripts/export_contract_manifest.py --check
uv run --no-sync pytest tests/test_runtime_contract_manifest.py tests/test_runtime_architecture_boundaries.py tests/test_runtime_service_import_purity.py tests/test_runtime_transport_client_compatibility.py -q
# web/ 下
npx tsc -b
node --test tests/runtimeContractFixture.test.ts tests/runtimeClientBoundary.test.ts tests/sourceGuard.test.ts
```

再跑新增协作测试、受影响的 manager/session/daemon/persistence/access 测试及真实浏览器脚本。Python 修改跑 Ruff；文档修改跑 MkDocs。Windows 本地验证，平台相关的 SQLite/进程关闭逻辑同时审查 Linux，并由 CI 覆盖。

## 10. 完成定义与进度

已定决策：**允许跨项目协作，默认开启**（保留显式关闭开关）；`project_scope` 继续约束 UI 的直接寻址，跨项目只经服务端投递。自动接续仍限定在轮次结算后。

| 项目 | 状态 |
| --- | --- |
| Web v2 交互原型确认 | 已确认 |
| 当前代码边界核对与实施计划 | 已完成 |
| P1 协作模型与 SQLite 存储 | 已完成（53 项测试通过） |
| P2 受控执行闭环 | 已完成（投递循环、排队、停止竞争、崩溃分类） |
| P3 Agent 工具与授权 | 已完成（两个工具、跨项目策略、代次轮换） |
| P4 服务契约与持久呈现 | 已完成（读与停止两个方法，契约与 TS 已生成） |
| P5 生产 Web UI | 已完成（会话引用、对话下方提示、协作面板、对方会话预览） |
| P6 收口 | 真实 daemon 子进程闭环与重启不重放通过（模型受控）；浏览器验收通过（`web/tests/collaborationUi.verify.ts`）；默认模型首次响应超时，两个界面缺陷待决策 |

### 真实运行故障修复与补充验收

指定会话 `8487b3cc8ee0` 的真实验收暴露了两个执行边界问题：

- 通用路径中间件按参数名把 `contact_session.target` 从 `project:thread`
  改成 `/project:thread`，导致目录查找误报项目未注册；并非跨项目策略拒绝。
  路径改写现仅限本地虚拟文件工具，协作标识和 MCP 等其他工具参数保持原样。
- 同步工具在工作线程调用投递循环的 `asyncio.Event.set()` 不安全；现通过所属事件循环的
  `call_soon_threadsafe` 唤醒。启动前和关闭后的通知不触碰循环，持久队列仍是恢复依据。

新增证据：

| 验证 | 结果 |
| --- | --- |
| `tests/test_collaboration_agent.py` | 修复前复现 `project-not-registered`；修复后生产 Agent 图的同步/异步、裸 ID/引用令牌四种路径均能联系并显式回复 |
| `tests/test_middleware_async.py` | 覆盖非文件参数透传和本地文件路径规范化，兼容对象/字典及同步/异步调用 |
| `tests/test_collaboration_runtime.py` | 工具线程通知在 owner loop 执行，启动前/关闭后通知安全 |
| `tests/test_collaboration_subprocess.py` | 两次独立 daemon 启动，真实 Agent 图、manager、WebSocket 与 SQLite；请求→读取目标文件→显式回复→来源接续，重启后历史相同且不重放；仅模型返回受控 |
| 默认模型实测 | 新 daemon、两个工作区内只读临时项目；`gemini-3.8-flash-high` 首次响应等待 180 秒超时，尚不能宣称真实模型闭环通过 |

可重跑真实模型验收：`uv run --no-sync python scripts/verify_session_collaboration.py --timeout 180`。
脚本在 `.tmp/collaboration-check/` 下隔离项目、目录库和 daemon，继承默认模型且禁止子 Agent；
不选择已有项目、不替换正在使用的 daemon，不输出凭据。成功必须验证来源已经消费回复，
不能仅凭请求为 `replied` 判通过；随后关闭连接、重启并核对历史与不重放。
超时或失败同样关闭自己的进程，Windows 必要时清理所启动的进程树，避免只结束 venv launcher。

本轮最终回归：协作（含真实图和子进程）、Agent factory、路径中间件、manager/session、
契约与架构相关测试 **386 passed**；修改文件 Ruff、契约导出 `--check`、MkDocs 构建和
`git diff --check` 均通过。MkDocs 仍有仓库既存的导航/锚点提示。
本轮没有修改正式 Web UI，也没有重新声称其浏览器验收通过。

### 浏览器验收（真实宿主 + 真实 daemon + 无头浏览器）

用仓库既有 CDP 设施（`web/tests/helpers/cdp.ts`）跑通了正式协作界面，
并另用外部 CDP 工具（chrome-devtools-mcp）在可见浏览器里复核了同一流程。
可重跑脚本：`web/tests/collaborationUi.verify.ts`

```powershell
cd web; $env:NODE_OPTIONS=''; $env:NODE_USE_ENV_PROXY=''
node tests/collaborationUi.verify.ts
```

它启动隔离 daemon（脚本模型，回复由闸门文件控制）、真实 `synapse-web-console` 宿主、
真实 dev server 与无头 Chrome，全部断言通过：

- 会话列表加载并选中来源会话；
- `@` 只提供**其他**会话（当前会话被排除），回车插入引用标签；
- 提交后提示条显示「我请对方 / 对方会话 / 对方处理中」，并给出「仅接收结果」「停止本次协作」；
- 右栏「协作」显示方向、消息类型与请求正文；
- 放行后提示条转为「已收到结论」，来源会话真的消费了对方的结论；
- 右栏「协作」显示交付结论本身，而不只是状态；
- 「对方会话」只读预览显示对方的真实转录，并提供「进入完整会话」；
- 1280/1024/820px 下无横向溢出，浏览器无异常。

宿主用 `--no-pairing` 启动（文档化的本地调试开关），配对流程本身由既有验收覆盖。

**本轮发现并修掉的缺陷**：`MentionFlyout` 只渲染 `file/skill/context` 三种分组，
`session` 分组从未被渲染——目录、图标、文案都齐了，但浮层永远不画它，
因此**读者在正式界面上根本无法引用另一个会话**，协作功能不可达。
现改为由 `mentionCatalog.MENTION_KIND_ORDER` 统一给出分组顺序（同时必须与
`RichComposer.offersFor` 的条目顺序一致，否则方向键会高亮/插入错行），
`composerMentions.test.ts` 的守护测试改为遍历该列表，并断言浮层使用它。

**仍未修、需要决策的两个缺陷**：

1. **等待中的回执不会自动出现。** 提示条只在会话切换与「来源轮次结算」时重读。
   若来源选择「仅接收结果」，对方结论到达时来源没有任何轮次结算，
   提示条会一直显示「对方处理中」；实测邮箱里请求已是 `replied`、
   来源投递为 `cancelled: scope_disabled`，界面仍显示等待。打开右栏「协作」
   （或切会话）后两处一起刷新，说明数据正确、只是缺少刷新触发。
   方向选择：(a) 有待处理请求时做有界轮询（UI-only，请求结束后自停）；
   (b) 服务端在写入回执时向来源会话补一个事件（改动 wire/事件契约，更贴合事件驱动）。
2. **对方会话预览在传输繁忙时只显示原始错误。** 连接 in-flight 达上限时，
   daemon 返回 `transport_busy`，预览面板把该错误原样显示，
   需要读者自己点「刷新对方会话」。建议改成可重试的明确状态并自动重试一次。

**仅接收结果路径**已在可见浏览器中人工验证：点击「仅接收结果」后，
邮箱显示请求 `replied`、来源投递 `cancelled(scope_disabled)`、来源代次 `receive_only`，
来源会话**没有**被自动续跑（与 P1 固定的规则一致）；差的就是上面的刷新触发。

### P5–P6 交付与验证证据

P5（`web/`）：

- 输入框 `@` 新增 `session` 引用：标签用会话名，令牌是
  `@session:<project_id>:<thread_id>`；候选来自侧栏已加载的会话，不额外读取；当前会话被排除。
- `runtime-client` 新增 `listCollaborations` / `stopCollaboration`，类型与分页默认值随契约导出。
- `stores/collaborationView.ts`：只读视图投影（角色由参与方决定、状态与消息类型有文案表、
  未知值不空白），并判定停止控制是否可用。
- `CollaborationNotice.tsx`：对话下方就地提示，按会话切换与**每轮结算**刷新，
  等待期间提供「仅接收结果 / 停止本次协作」。
- 右栏「协作」面板（完整列表与控制）与「对方会话」面板（只读预览 + 进入完整会话）。

**为什么提示条没有做成 transcript 行**：transcript 是运行时的投影，而本地合并的行会被下一次
历史加载清空、只在下次刷新时才回来（回执会时隐时现）。就地提示条位于同一阅读列、紧邻输入框，
且与会话切换/结算同步，因此没有这个时序问题。

P6（受控端到端，`tests/test_collaboration_e2e.py`）：两个真实 `RuntimeManager`（两个项目）
加真实 `submit_ref`、提交锁、并发额度与结算等待，只有 turn 本身受控：

- 请求真的从 p1 的会话进入 p2 的会话，来源在目标工作期间保持空闲；
- 目标自己的用户轮次先跑，协作投递排队且不抢占，结束后按序执行；
- 停止通过 manager 真的取消了来源的接续轮次，且不触碰目标已完成的工作；
- 重启（同一邮箱、全新组合）把中断投递分类为 `interrupted`、请求置 `failed`，**不重放**。

```
uv run --no-sync pytest tests/test_collaboration_*.py tests/test_runtime_contract_manifest.py \
  tests/test_runtime_architecture_boundaries.py tests/test_runtime_service_import_purity.py \
  tests/test_runtime_manager.py tests/test_session_runtime.py -q                    # 240 passed
cd web; npx tsc -b; npm test                                                        # 1195/1196
```

后续已补真实 daemon 子进程验收与浏览器 `.verify.ts` 级验收，详见上方补充证据。
仍缺默认模型成功闭环（首次响应超时，属外部服务问题）；两个界面缺陷
（等待中回执不自动刷新、预览的传输繁忙错误）已记录待决策，未修复。

### P2–P4 交付与验证证据

新增 `collaboration/{coordinator,runtime,tools,service_adapter}.py` 与 `service/collaboration.py`；
接线到 `daemon/{application,config,entry}.py`、`sessions/manager.py`、
`service/{local,ports,access,contract_registry}.py`、`transport/protocol.py`；
契约清单与 `web/src/runtime-client/contract.generated.ts` 已重新生成。

已用测试固定的行为：

- 请求→目标执行→回复→来源接续的完整闭环；等待结算不占用并发额度。
- 目标忙碌时保持 FIFO 且不抢占；不同接收方互不阻塞；同一接收方同时只有一个 owner。
- 停止先赢则不启动来源接续；接续先赢则必须取消该轮次；目标自身的运行工作不被触碰。
- 停止后迟到的回复仍被保存（请求为 `replied`），但来源投递为 `cancelled`，不会重启。
- 目标结算却未回复 → 请求 `failed`（`no_result`），不伪造成功。
- 崩溃后 `recover_uncertain` 只做分类、`settle_interrupted` 标记为已结束，绝不重放。
- 工具身份来自可信运行上下文：模型不能伪造来源、请求 ID 或工作代次；角色与状态双重校验；
  同一 tool call 重试幂等。
- 新用户轮次把上一代次降为 `receive_only`（请求保留、结果不再自动启动来源），
  显式停止才是 `revoked`；投递轮次绝不轮换代次。
- 跨项目默认允许；`--no-cross-project-collaboration` 关闭后工具与投递都拒绝。
- 客户端只能读取与停止：没有任何 wire 方法能发起请求或投递消息。

```
uv run --no-sync pytest tests/test_collaboration_*.py tests/test_runtime_contract_manifest.py \
  tests/test_runtime_architecture_boundaries.py tests/test_runtime_service_import_purity.py \
  tests/test_runtime_transport_client_compatibility.py tests/test_runtime_manager.py \
  tests/test_session_runtime.py -q                                        # 307 passed
uv run --no-sync python scripts/export_contract_manifest.py --check       # up to date
cd web; npx tsc -b; node --test tests/runtimeContractFixture.test.ts \
  tests/runtimeClientBoundary.test.ts tests/sourceGuard.test.ts           # 22 passed
```

已知的既有失败（与本次改动无关，纯净检出同样失败，已用 `git stash` 复核）：
`tests/test_runtime_service_contracts.py` 的两项源码扫描断言，
以及 `tests/test_runtime_daemon_s8.py::test_mcp_rebinding_writes_only_what_was_asked_and_reuses_a_live_pool`。

P2–P4 明确未做：未改正式 Web UI；未提供“发起请求”的 wire 方法（它是 Agent 动作）；
未做群聊、永久同步与多级转派。

### P1 交付与验证证据

新增 `src/synapse/runtime/collaboration/{__init__.py,model.py,store.py}` 与扁平测试
`tests/test_collaboration_model.py`、`tests/test_collaboration_store.py`。存储为单一
`collaboration.sqlite`（`user_version=1`，未知版本只读拒绝），全部写入走 `BEGIN IMMEDIATE`，
请求/消息按服务端操作键幂等，回执与投递意图同事务写入。

已用测试固定的规则：

- 停止（`REVOKED`/`RECEIVE_ONLY`）取消待投递项、把已领取项保留为 `review`，不重放；
- 新工作代次撤销旧许可，但重试旧代次不会撤销当前有效许可；
- 每目标 FIFO 且同一目标同时只有一个 owner（跨连接并发领取验证）；
- 交付方向按消息类型校验：终态请求不能启动新目标工作，最终回复仍可送达来源；
- 补充答复与最终回复会作废已过期澄清，`claimed`/`running` 的未知项不被静默抹掉；
- 重启只做有界核验（`recover_uncertain`），待投递保留、未决领取不自动重试。

```
uv run --no-sync pytest tests/test_collaboration_model.py tests/test_collaboration_store.py -q  # 53 passed
uv run --no-sync ruff check src/synapse/runtime/collaboration tests/test_collaboration_*.py      # All checks passed
uv run --no-sync pytest tests/test_runtime_architecture_boundaries.py \
  tests/test_runtime_service_import_purity.py tests/test_runtime_contract_manifest.py -q         # 65 passed
```

P1 明确未做：未装配 daemon、未注册 Agent 工具、未新增 wire 方法、未改正式 Web UI；参与方检查
只是纵深防御，不是项目 ACL。

完成必须证明：A 无需离开当前会话就能协作，B 忙时不被抢占，回执可恢复且不重复触发，许可有效时自动继续，停止后绝不被迟到结果重启。任何“状态未知”必须如实呈现，不能用重试掩盖。
