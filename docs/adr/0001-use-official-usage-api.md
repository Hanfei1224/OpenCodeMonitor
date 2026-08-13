# ADR 0001: 使用供应商官方 usage 接口，而非本地代理日志

- 状态：已采纳
- 日期：2026-08-12

## Context

监控程序需要展示 OpenCode Go 套餐三个窗口（5小时 / 本周 / 本月）的用量百分比。两个候选数据源：

1. **CC Switch 本地 SQLite**（`~/.cc-switch/cc-switch.db` 的 `proxy_request_logs` 表）—— 记录每次经本地代理转发的请求 token 用量，并按本地价格表折算成 USD 花费。
2. **供应商官方接口** `GET https://opencode.ai/zen/go/v1/usage` —— 直接返回官方计费口径的百分比。

实测发现两者口径不一致：官方月度百分比（26%）明显高于按本地日志推算的 ≈22%，周度（9%）也高于本地推算的 ≈4%。本地价格表无法复现官方计费。

## Decision

使用官方接口 `GET /zen/go/v1/usage`，认证同时携带 `Authorization: Bearer` 与 `x-api-key` 两个请求头。直接使用返回的 `percent` 字段。不在运行时读取本地代理日志。

## Consequences

- 展示的数字与供应商官网仪表盘一致，权威可信。
- 需要在 `config.json` 中配置有效 API Key。
- 本地日志不再作为运行时数据源。
