# 知识发布端 HTTPS 运维

此入口仅提供 `/knowledge/v1/` 下的读取服务。工作台仍监听服务器回环地址，经 SSH 隧道访问；Nova 的页面与后台继续运行在原环境。

## 部署边界

- nginx 模板：`knowledge-nginx.conf.template`，替换 `PUBLIC_IP` 为部署实例公网 IPv4。
- 80 端口仅允许 ACME HTTP-01 挑战，其余路径返回 404。443 端口只有知识协议路径，其他路径返回 404。
- 知识协议在应用层校验独立只读 Bearer token；发布包与撤回清单还须通过 Ed25519 签名校验。TLS 不能替代这两项校验。
- 私钥仅在发布端；Nova 配置只读 token 和可信公钥。禁止将私钥、凭据文件放进仓库、镜像或公开报告。
- Nova 的 `INSPIRATION_PUBLISHER_URL` 必须带 `/knowledge/v1`，其余两项为 `INSPIRATION_READ_TOKEN` 与 `INSPIRATION_TRUSTED_KEYS_JSON`。

## IP 证书与续期

本次试点使用 Certbot 5.4.0 与 Let's Encrypt shortlived IP 证书，无需新增域名。IP 证书有效期较短，不能只申请一次便长期放置。

- Certbot 环境：`/opt/omnisignal/certbot-venv`。
- webroot：`/var/lib/omnisignal-acme`。
- 证书名：`omnisignal-knowledge`。
- nginx 使用 `/etc/letsencrypt/live/omnisignal-knowledge/` 下的证书文件。
- 安装 `knowledge-cert-renew.service` 和 `.timer` 至 `/etc/systemd/system/`，执行 `systemctl daemon-reload`、`systemctl enable --now knowledge-cert-renew.timer`。
- 定时器每天检查两次；续期成功后先执行 `nginx -t`，再 reload。修改规则后应运行 `certbot renew --dry-run --run-deploy-hooks`。
- 运维检查：定时器须 active，HTTPS 验证须正常，未带 token 读取应为 401，管理路径应为 404。续期失败查看 `journalctl -u knowledge-cert-renew.service`，不要用关闭证书验证作为修复。

官方能力依据：[Certbot 与 IP 证书](https://letsencrypt.org/2026/03/11/shorter-certs-certbot/)、[短期和 IP 证书正式可用](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability/)。

## 回滚与数据

应用版本切换前备份 SQLite 与 env，记录旧镜像。回滚应用优先使用原镜像和现有卷，不能为恢复页面覆盖已产生的新数据。知识发布是不可变版本加增量；正式撤回走签名撤回接口。撤回会使下游不可用，但不会自动物理删除历史包，应另行执行保留期策略。
