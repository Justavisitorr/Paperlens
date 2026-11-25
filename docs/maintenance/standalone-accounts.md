# 独立账户与多人工作区

独立服务为每个账户分配单独的文献库、模型配置、任务和阅读记录。读者使用自己的模型服务账户。部署者提供用于登录的邮件服务或 GitHub OAuth 应用。

## 本机试用

在仓库根目录安装 Python 依赖后运行以下命令。

```bash
python -m paperlens.standalone --data .standalone --origin http://127.0.0.1:8080 --port 8080 --dev-mail
```

访问 `http://127.0.0.1:8080`，填写测试邮箱，再从 `.standalone/test-mail/` 中读取本次验证码。登录后可以导入论文、阅读原始 PDF，并配置自己的模型连接。Windows 用户也可以在首次运行本机版后双击 **启动独立服务验收.cmd**。

## 自托管配置

[部署示例](../../deploy/standalone/) 包含应用容器、Caddy 配置和环境变量模板。构建前先在 `frontend/` 运行 `npm ci` 和 `npm run build`。为应用分配可持久化的数据目录，并保管好其中的账户数据库、加密材料和文献。备份应覆盖整个数据目录。

- 将 `.env.example` 复制为自己的 Compose 配置，填写 HTTPS 来源、数据目录、凭据目录和运行配置文件路径。
- 根据 `runtime.env.example` 配置 SMTP。如需 GitHub 登录，填写自己的客户端 ID 并通过凭据文件提供对应密钥。使用的回调路径为 `/auth/github/callback`。
- 将密钥文件放在源码目录以外。在代理后部署时，按配置模板填写实际可信代理地址。

测试邮箱模式仅供回环地址上的本机验证。外网登录需要实际可用的邮件服务或 GitHub OAuth 配置。
