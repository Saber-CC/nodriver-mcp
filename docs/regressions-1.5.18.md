# 1.5.18 回归修复

依赖仍为官方 `nodriver==0.50.6`、`mcp==2.3.0`。无需修改 nodriver 或使用 fork。

## 修复内容

1. **带元素参数的脚本超时**：将 `Runtime.callFunctionOn` 的同步调用与
   `Runtime.awaitPromise` 分开。取消仍在执行的同步调用时发送
   `Runtime.terminateExecution`，终止 JavaScript 后再释放对象组。
   Promise 等待超时时先检查 renderer 是否响应，仅在忙于执行时中断，避免对
   空闲 Promise 无条件终止执行。保留元素所在 JavaScript world 和页面全局变量，
   不额外启用 Runtime/Debugger 域。
2. **trace 并发停止与取消**：停止开始后，由停止调用持有录制状态直到流读取和保存
   完成。自动录制调用的取消不再提前释放状态；停止调用最终清理前也校验状态归属，
   避免旧调用清空新录制。
3. **source map 的 script ID 复用**：结合执行上下文、脚本 hash、URL 和映射信息识别
   脚本，并给每个版本分配独立缓存身份。日志收集时保留脚本描述及所属目录；导航与
   上下文销毁后，旧日志仍使用旧描述。旧映射已有缓存时继续使用；未缓存的外部映射
   不通过新页面重新加载，保留生成位置及诊断信息。
4. **新跨进程 iframe 的初始化**：设置媒体特性或额外 HTTP headers 后，使用官方
   `Target.setAutoAttach(waitForDebuggerOnStart=true)` 监视该页面及递归 iframe。
   在新 iframe 恢复运行前应用设置，再调用 `Runtime.runIfWaitingForDebugger`。
   通过独立的 nodriver 连接处理这些启动会话，避免混淆正常网络采集的连接归属。
   后续修改及重置同时同步到这些会话；关闭页面/浏览器时释放连接和暂停状态。
5. **重复填充相同邮箱**：只对支持选区的 input 类型和 textarea 调用
   `setSelectionRange`。相同邮箱值仍验证焦点和值，批量填充继续处理后续字段。

## 验证

在原始 `5a5ce42` 源码上独立运行新增的 5 个真实 Chrome 回归用例，5 项均能复现，
其中脚本用例同时复现同步循环及 await 后的循环。修复后的用例验证超时后继续读取
`42`、trace 停止期间取消、新 renderer 复用 script ID 及旧日志查询、未调用
`list_frames` 时 iframe 首批资源/媒体特性、重复邮箱及后续批量字段。

另外覆盖同步调用与 Promise 的取消分支、旧 trace 的最终清理归属、source map
并发加载及过期上下文、iframe 初始化失败后恢复运行与连接清理。

2026-10-20 在 macOS、Chrome 154 下完成全量验证：**170 项，169 通过，1 跳过**。
唯一跳过项是 Chrome 未提供网站 `navigator.modelContext` API。测试包含隔离安装、
真实 Chrome、stdio MCP 客户端及真实 DevTools Trace Engine；并行页面的正常 Promise
不会因另一页的空闲 Promise 超时而失败。锁文件、修改文件的 Ruff 检查/格式检查、
diff 检查及 wheel/sdist 构建通过。
