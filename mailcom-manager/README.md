# MailCom Hub

独立的本机 mail.com 邮箱管理网站，不读取或修改主程序 `app/` 的代码、配置或 MongoDB。

## 功能

- 批量导入 `邮箱----密码`
- Windows DPAPI 当前用户加密存储密码
- 批量测试 IMAP 登录
- 只读查看 INBOX、Spam 和 Junk
- 提取邮件中的六位验证码
- 自动使用本机 `127.0.0.1:7897` SOCKS/Mixed 代理（监听存在时）
- 搜索、复制邮箱和删除本地记录
- REST API 与 Swagger 文档
- 通过 SSH 将主邮箱、最新密码和别名全量推送到独立服务器

## 启动

双击根目录中的 `启动MailCom管理器.cmd`，浏览器会打开：

```text
http://127.0.0.1:3211
```

停止服务时双击 `停止MailCom管理器.cmd`。

## API

- `GET /api/health`
- `GET /api/stats`
- `GET /api/accounts`
- `POST /api/accounts/import`
- `POST /api/accounts/{id}/test`
- `POST /api/accounts/test-all`
- `GET /api/accounts/{id}/messages`
- `GET /api/accounts/{id}/latest-code`
- `GET /api/mail/latest?email=邮箱地址`
- `GET /api/export/registration-lines`
- `GET /api/export/registration-items`：供本机注册机同步分裂邮箱及其主邮箱归属。
- `GET /api/accounts/{id}/aliases`
- `POST /api/accounts/{id}/aliases/import`
- `DELETE /api/aliases/{id}`
- `DELETE /api/accounts/{id}`

API 文档：`http://127.0.0.1:3211/docs`

## 推送服务器

首页点击“推送服务器”，填写服务器主机、SSH 端口、用户名和密码。每次推送都是全量幂等快照：服务器上的主邮箱、密码和别名会与本机当前状态保持一致。

- SSH 密码只存在于本次浏览器请求和后端内存中，不写入配置、数据库或日志。
- 邮箱密码从本机 DPAPI 解密后经 SSH 加密通道传输，并在服务器端使用独立 Fernet 密钥重新加密。
- 服务器必须预先部署 `/opt/mailcom-manager-public` 接收端。
- 推送接口仅绑定在本机 MailCom Manager；公网 Nginx 不开放同步、账号列表、导入或删除接口。

注册机导入格式：

```text
邮箱----http://127.0.0.1:3211/api/mail/latest?email=URL编码后的邮箱
```

网页中的“复制注册机格式”会一次复制全部邮箱。

## 分裂邮箱 / 别名管理

先在 mail.com 中创建并确认别名能够投递到对应主邮箱，再在网页中点击该主邮箱的“分裂管理”导入别名。系统不会为别名保存第二份密码，而是复用主邮箱的 DPAPI 加密凭据。

每个主邮箱和别名都有独立注册机地址：

```text
ALIAS_EMAIL----http://127.0.0.1:3211/api/mail/latest?email=ALIAS_EMAIL_ENCODED
```

别名读取会对 `To`、`Delivered-To`、`X-Original-To` 和 `Envelope-To` 收件人字段进行精确匹配，只返回投递给该别名的邮件，避免多个别名之间串验证码。“复制注册机格式”会同时导出主邮箱和所有别名。

## 数据

- SQLite：`data/mailcom.db`
- 日志：`data/server.stdout.log`、`data/server.stderr.log`
- 密码：DPAPI 加密 BLOB，仅当前 Windows 用户可解密

可用 `MAILCOM_IMAP_PROXY=socks5://HOST:PORT` 覆盖默认代理；留空时直接连接。
