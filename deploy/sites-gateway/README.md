# Sites 网关

本目录包含把已登录的 Sites 用户连接到 PaperLens 后端的网关。独立账户服务的入口和配置见[独立服务说明](../../docs/maintenance/standalone-accounts.md)。

`npm test` 运行使用内存存储的网关测试。`npm run build` 将 worker 写入 `dist/server/index.js`。已有 Sites 工作区可以提供自己的 `.openai/hosting.json` 供构建时一并复制。网关与后端的配对由部署者在自己的环境中完成。
