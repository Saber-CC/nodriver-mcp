# 1.5.20：取消初始化后的 source map 重试

1.5.19 的首次采集初始化在 `Debugger.enable` 返回前被取消时，Chrome 可能已经开启
Debugger 并回放脚本事件，但服务端 `active` 仍为 `False`。清理因而跳过
`Debugger.disable`，旧采集器及其目录被丢弃；重试的启用请求不会再次收到已有脚本的
描述，旧函数新输出的日志也无法映射到源码。

修复增加独立的 `enabling` 状态，在发送 `Debugger.enable` 前记录，并在返回后转为
`active`。停止采集时同时检查这两个状态；取消发生在响应返回前也会关闭本次管理的
Debugger，再移除采集器。外部 Debugger 回调的保留规则保持有效。

新增真实 Chrome 用例通过事件和响应屏障固定两种时序：脚本事件正在回放、以及 Chrome
已返回响应但调用尚未恢复。两者在原始 `67ebba2` 上均复现源码映射丢失；修复后重试
会重新收集已有脚本，随后调用已有函数产生的新日志能映射到 `.ts`。
单元测试还覆盖在启用、配置和 Runtime 启动阶段取消，验证原始取消信号、回调清理、
状态复位与重试，并检查外部 Debugger 回调不会被清除。

2026-10-20 在 macOS、Chrome 154 下完成全量验证：**177 项，176 通过，1 跳过**。
唯一跳过项仍是 Chrome 未提供网站 `navigator.modelContext` API。隔离安装、真实
Chrome、stdio MCP 客户端、DevTools Trace Engine、锁文件、修改文件的 Ruff 检查/
格式检查、diff 检查及 wheel/sdist 构建通过。
