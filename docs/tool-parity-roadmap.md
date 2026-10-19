# 同名工具能力补齐与优化计划

对照日期：2026-10-19。比较基线为 chrome-devtools-mcp 1.11.0
（`2ab93a3`）和 nodriver-mcp 1.4.2；本轮正确性修复发布于 1.4.3。
本计划聚焦两个项目同名工具的缺失能力和已有功能优化。

底层继续使用官方 nodriver 0.50.6 的原生能力和生成的 CDP 接口。
保留现有工具调用方式；新增参数应兼容旧调用。每次迭代完成一个明确目标，
通过相关回归后再标记完成，避免一次扩大到整套工具迁移。

## 进度与建议顺序

以下复选框是进度记录，后面的章节给出各项验收标准。

- [x] FIX-01：修复 `press_key` 组合键、加号键和失败后的按键释放。1.4.3。
- [x] FIX-02：修复 `click_at` 坐标双击，共用 UID 点击的可信事件实现。1.4.3。
- [x] ADV-01：页面工具支持每次调用的稳定 `page_id` 和多页面并发。1.5.0。
- [x] OPT-01：统一交互后的导航与 DOM 稳定等待。1.5.1。
- [x] REQ-01：补齐表单控件的指定状态设置。1.5.2。
- [x] REQ-02：补齐多文件上传和文件选择器处理。1.5.3。
- [x] REQ-03：补齐未捕获 JavaScript 异常和基础调用栈。1.5.5。
- [x] REQ-04：补齐脚本执行时的弹窗处理策略。1.5.6。
- [x] OPT-02：优化长文本输入和批量表单填充。1.5.7。
- [x] OPT-03：优化脚本超时和大结果输出。1.5.8。
- [ ] OPT-04：完善网络请求诊断和正文可用状态。
- [ ] OPT-05：增加截图返回尺寸限制。
- [ ] OPT-06：完整保留性能 trace 并返回录制信息。

## 已完成的正确性修复

### FIX-01：press_key

原实现只发送 `key`，未附带修饰键位、物理键码；`Control++` 会被拆成空键。
发送失败时也没有释放已经按下的修饰键。

1.4.3 使用官方 `Input.dispatchKeyEvent` 发送修饰键状态、`code`、虚拟键码和
必要文本。支持字母、数字、常用标点、导航键、功能键、左右修饰键及常见键盘别名；
`+` 可作为最终目标键。非法组合在选取页面前失败。
发生错误或取消时，继续尝试释放目标键和所有修饰键，并保留最初的错误。
macOS 文本编辑快捷键附带 Chrome 的编辑命令，保持平台语义。

验收包括真实 Chrome 的 `Control+A`、`Control+Shift+x`、`Control++`、
`Control+Shift++`、`Shift++`，以及全选、删除、字符输入、Tab/Shift+Tab、
Enter 和 `type_text(submit_key=...)`。故障注入覆盖发送失败、释放失败和取消。

### FIX-02：click_at

原实现把坐标双击作为两次 `clickCount=1` 的单击发送，没有产生 `dblclick`。

1.4.3 将 `click` 与 `click_at` 统一到同一鼠标事件助手。
双击发送两组按下/释放，点击计数依次为 1、2；释放时 `buttons=0`。
真实 Chrome 验收 `mousedown → mouseup → click` 和完整双击事件序列，
并检查 `isTrusted=true` 和 `include_snapshot` 返回结果。

## 必须补齐的缺失能力

### REQ-01：表单控件状态

已完成于 1.5.2。参考 `chrome-devtools-mcp/src/tools/input.ts` 的
`fillFormElements` 与 toggle 分支，增加布尔值及兼容的 `"true"`/`"false"`。
先读取实际状态，已满足时不重复点击；通过可信 CDP 点击改变状态，并核验
原生 checked/indeterminate 或 aria-checked。选中的 radio 不允许直接取消，
禁用、只读、状态未变化及非法输入明确失败。真实 Chrome 覆盖原生/ARIA 控件、
幂等操作、混合状态、radio 分组、批量填充及同进程/跨进程 iframe；
真实 stdio 客户端覆盖布尔输入与数字输入拒绝。

涉及 `fill`、`fill_form`。增加原生 checkbox、radio，以及 ARIA checkbox、
radio、switch 的指定状态设置。

验收：

- 接受并验证明确的布尔状态；radio 的操作遵循浏览器允许的选择行为。
- 重复设置为同一状态保持幂等，避免用无条件切换代替设置目标状态。
- 原生和 ARIA 控件均核验操作后的实际状态；无法达到目标时返回明确原因。
- 保留现有文本、select、空字符串、受控输入框与 iframe 行为。

### REQ-02：文件上传

已完成于 1.5.3。参考 `chrome-devtools-mcp/src/tools/input.ts` 的 `uploadFile`，
保留 `file_path` 并新增 `file_paths`，文件输入框委托官方 `Element.send_file(*paths)`。
普通按钮先设置 Page 文件选择器拦截再可信点击，通过事件里的 frame 和 backend
node 定位输入框，用官方 DOM 命令设置文件。按页面串行接管选择器，校验本地路径
和多文件能力，所有退出路径清理回调及拦截状态。真实 Chrome 覆盖旧调用、多文件、
按钮、超时后重试、数量不匹配后重试及同进程/跨进程 iframe；
故障注入验证取消和回调清理，真实 stdio 客户端验证新参数。

涉及 `upload_file`。新增多个文件路径的调用方式，并保留现有 `file_path`。
当 UID 指向普通上传按钮时，支持点击并接管文件选择器。

验收：

- 单文件旧调用、多文件输入框、普通按钮触发的选择器都能上传。
- 文件不存在、非 multiple 输入框传入多个文件、选择器未出现等情况明确失败。
- 正确处理 iframe 所属连接，且在成功、失败、超时时移除临时监听器。

### REQ-03：异常与调用栈

已完成于 1.5.5。参考 `chrome-devtools-mcp/src/collectors/PageCollector.ts`
的 `Runtime.exceptionThrown` 与 `src/formatters/ConsoleFormatter.ts`，
将未捕获异常和 Promise 拒绝作为 error 消息采集；保存来源、脚本位置、上下文和
原生调用栈，异常之后被处理时保留撤销标记。列表新增 `include_stack_traces`，
详情默认展示堆栈并可关闭。仍须显式开启 Runtime 采集，所有新增回调跟随连接清理。
真实 Chrome 覆盖异步异常、Promise 拒绝、来源和行列号、导航后保留及禁用清理；
iframe 往返重建测试覆盖异常采集恢复，stdio 客户端验证新参数与详情。

涉及 `list_console_messages`、`get_console_message`。当前主要收集 `console.*`，
需要接入未捕获 JavaScript 异常，保留基础堆栈、脚本 URL、行号和列号。

验收：

- 能看到普通 console 消息、异步未捕获异常，以及浏览器报告的未处理 Promise 拒绝。
- 可按需返回堆栈，列表保持紧凑，消息详情保留诊断字段。
- 保留显式开启采集的方式；关闭或淘汰连接时完整清理新增回调。
- iframe 往返导航和连接重建后重新开启采集仍然有效。

### REQ-04：脚本弹窗策略

已完成于 1.5.6。参考 `chrome-devtools-mcp/src/tools/script.ts` 和
`src/utils/WaitForHelper.ts`，脚本新增 accept/dismiss/manual 策略及 prompt 内容，
默认接受。按页面跟踪弹窗状态和连接归属，重复协议通知去重；连续相同弹窗仍分别处理。
输出明确区分 completed/interrupted/failed/timed_out，保留 JSON 值代码块。
执行等待新增超时，手动弹窗立即返回详情；被弹窗阻塞的交互快速失败，诊断工具继续可用。
临时回调、处理任务和原生协议等待在退出时清理，保留每连接的轻量弹窗状态监听。
真实 Chrome 覆盖同步/异步/连续弹窗、prompt、手动处理、跨页面与跨进程 iframe、
超时后继续执行和 JavaScript 错误原因；stdio 客户端验证同样的策略与状态。

涉及 `evaluate_script`。支持明确的 accept、dismiss 和 prompt 内容策略，
区分脚本完成、被弹窗中断、失败和超时。

验收：

- 同步和异步脚本中的 alert、confirm、prompt 都有明确可配置行为。
- 无需等脚本完成才能得知页面正在被弹窗阻塞。
- 保持手动 `handle_dialog` 的调用能力；临时处理器在所有退出路径清理。
- 与 OPT-03 的执行超时配合，避免遗留等待影响后续工具调用。

## 已有能力优化

### OPT-01：统一交互等待

已完成于 1.5.1。参考 `chrome-devtools-mcp/src/utils/WaitForHelper.ts`，
使用官方 Page/Target 事件在动作前开始观察，动作后进行导航探测、有时限的
导航等待和 MutationObserver 静默窗口。批量填充和输入后的提交键共用一次等待，
附带快照在等待之后生成。持续变化的 DOM 返回超时限提示，不等待网络空闲。
脚本新增 `wait_for_stable=false`；未处理弹窗返回明确中断原因，临时回调与观察器
在所有退出路径清理。真实 Chrome 覆盖延迟 DOM 更新、点击导航、新页面、
持续变化页面和弹窗中断；故障注入覆盖取消、初始化失败和观察器超时。

涉及 `click`、`click_at`、`fill`、`fill_form`、`press_key`、`type_text`、
`navigate_page` 和 `evaluate_script` 等交互工具。

在动作前准备相关观察；动作后按实际发生的导航、DOM 变化、弹窗、新页面返回结果。
等待必须有上限，并允许只读脚本跳过 DOM 稳定等待。

验收：点击导航或异步更新后，紧接着读取快照能观察到稳定结果；
长连接和持续轮询页面能按时返回；失败后清理监听器，保留可信输入事件。

### OPT-02：填充效率

已完成于 1.5.7。参考 `chrome-devtools-mcp/src/tools/input.ts` 的一次批量等待和
其 Puppeteer 25.14.0 Locator 的 100 字符阈值：短文本委托官方 `Element.send_keys`，
长文本使用官方 `Input.insertText` 一次发送，保留短文本按键事件。统一读取控件元数据，
检查焦点、可编辑状态、长度限制并验证最终值；空值继续通知页面模型。
批量填充复用一次 frame 刷新和一次动作等待，首个失效 UID、导航或弹窗后停止，
保留完成数量、当前错误和跳过项，即使附带快照失败也不丢进度。
真实 Chrome 验证长中文/emoji 及受控模型、单次插入事务、短文本 keypress、
textarea/contenteditable、约束保护和三类中断；stdio 客户端覆盖长文本与部分完成报告。

涉及 `fill`、`fill_form`。评估使用官方 `Input.insertText` 减少长文本逐字符发送；
批量填充复用准备步骤和一次操作后的等待流程。

验收：长文本、中文、emoji 和受控表单的页面模型均正确更新；
出现导航、失效 UID 或弹窗时及时停止并报告已完成项目，不继续静默填充。

### OPT-03：脚本超时与大结果

已完成于 1.5.8。参考 `chrome-devtools-mcp/src/tools/script.ts` 的格式及文件输出，
保留兼容的 auto 格式，另可显式指定 function/script。auto 先只解析源码，再执行一次，
避免 IIFE 重复调用和副作用重试；无需启用 Runtime 域。官方 Runtime.evaluate 的
原生 deadline 配合工具等待超时，所有退出路径释放临时对象组；不对普通 Promise 超时
无条件终止整个 renderer。新增 file_path 和 max_output_chars，完整 JSON 可显式保存，
大结果自动保存到持久 artifacts/scripts 目录，返回路径、总长度及明确标注的预览。
真实 Chrome 覆盖同步循环、Promise 超时后重用、脚本格式、一次副作用、CSP 与文件输出；
真实 stdio 客户端覆盖新参数和完整文件读取。

涉及 `evaluate_script`。增加执行超时、结果文件输出和明确的截断说明。

验收：长期等待的 Promise 能结束工具等待；超时与脚本异常可区分；
大结果完整写入调用方指定文件，返回概要与路径；清理遗留等待和临时处理器。
从文件加载脚本另见 ADV-03。

### OPT-04：网络诊断

涉及 `list_network_requests`、`get_network_request`。补齐重定向链，
关联额外请求/响应头事件，完善正文可用状态和输出长度控制。

验收：重定向记录可追踪；请求头和响应头补充信息正确关联；
明确区分请求未完成、正文已清理、协议失败；现有稳定请求 ID、过滤、分页、
文件导出和 iframe 连接重建回归继续通过。

### OPT-05：截图尺寸

涉及 `take_screenshot`。增加可选最大宽高，按比例限制返回图片尺寸。

验收：页面、整页、元素截图的比例正确，满足两个尺寸上限；
PNG/JPEG/WebP 均可用；省略新参数时保留现有分辨率和图片返回行为。

### OPT-06：性能 trace 保存

涉及 `performance_start_trace`、`performance_stop_trace`。
即使调用方未指定文件路径，也应完整保留 trace，并返回可访问路径、录制时长和配置。

验收：自动停止和手动停止均可获得完整文件；跨页面切换仍使用原始连接；
失败后清理录制状态与监听器。完整性能指标和 Insights 分析另见 ADV-06。

## 按需求推进的增强

- [x] ADV-01：1.5.0 已为 63 个页面工具加入显式 `page_id`，包含已有的选择/关闭工具。
  使用 Chrome target ID 作为稳定字符串 ID，兼容原有数字索引。
  参考 `chrome-devtools-mcp/src/ToolHandler.ts` 的入口选页与 `request.page` 传递方式，
  使用调用上下文固定页面和 frame；嵌套操作、快照、UID、采集与仿真按目标页面工作，
  显式 ID 不修改全局选页，关闭后的 ID 明确失败。录屏按页管理，性能 trace 保留
  Chrome 的浏览器级单实例限制并校验停止时的页面归属。
  真实 Chrome 覆盖两页并发填充/输入、图片、frame、导航、仿真、日志、网络与旧 ID；
  stdio MCP 客户端验证并发调用及新参数 schema。
  1.5.4 对照 `chrome-devtools-mcp/src/McpPage.ts` 的 focused-page 初始化补强后台焦点：
  原先后台页面 `document.hasFocus()` 为 false；现在通过官方 Emulation 命令保持
  页面焦点语义，并验证选页、导航与仿真重置后仍生效。初始化失败可重试，连接退役清理缓存。
- [ ] ADV-02：`emulate` 增加 forced-colors、prefers-contrast、prefers-reduced-motion
  和额外 HTTP headers。当前可先通过通用 CDP 工具调用底层能力。
- [ ] ADV-03：`evaluate_script` 支持从脚本文件加载源码，明确与内联源码的互斥关系。
- [ ] ADV-04：控制台支持完整 source map 定位，并按需启用相关调试能力。
- [ ] ADV-05：`evaluate_script`、`list_pages` 和控制台工具支持扩展 ServiceWorker
  的发现、指定执行和日志筛选。
- [ ] ADV-06：性能 trace 的 Web Vitals 摘要和深入分析。需要单独评估分析器，
  与基础录制、文件保存分开推进。

## 后续迭代的回归基线

保持 UID 与坐标点击的可信事件、中文和空值填充、快捷键实际编辑效果、
CDP 缩写映射与错误详情、主 frame 搜索、iframe 连接切换及采集恢复有效。
工具参数或响应发生变化时，验证真实 MCP 客户端调用；依赖或打包变化时，
补做隔离安装检查。完成一项后更新此文档的状态、版本和验证记录。
