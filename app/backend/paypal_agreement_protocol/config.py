USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


SCREEN = {
    "colorDepth": 24,
    "pixelDepth": 24,
    "height": 900,
    "width": 1440,
    "availHeight": 860,
    "availWidth": 1440,
}


VIEWPORT = {"width": 1280, "height": 720}

TEALEAF_APP_KEY = "76938917d7504ff7a962174c021690bd"  # gitleaks:allow
HCAPTCHA_SITEKEY = "884d15d9-b649-4bbb-8d1c-2d6f0eed75eb"  # gitleaks:allow


# 1024proxy 动态代理池。格式：host:port:username:password
# 默认不启用；生产环境不要把代理账号密码写入代码仓库。
# 如需启用，请通过环境变量 PAYPAL_PROXY_POOL 或 PAYPAL_PROXY_URL 注入：
#   PAYPAL_PROXY_ENABLED=1
#   PAYPAL_PROXY_POOL='host:port:username:password,host2:port:username:password'
# 或在部署平台的 secret manager 中配置同名变量。
PROXY_ENABLED = False
PROXY_POOL: list[str] = []
