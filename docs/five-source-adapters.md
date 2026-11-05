# 五来源图片适配接口与验收边界

2026-11-05。此文档记录新增适配模块，不表示五个来源已经在线稳定采集，也不表示每天交付 300 张已验收。

## 文件与职责

- `src/omnisignal/workbench/source_adapters.py`：来源元数据、项目身份、可见页面元数据解析、浏览器观察归一化、Pexels 官方照片 API。
- `tests/test_workbench_source_adapters.py`：来源隔离、项目去重、权限保持未知、视频排除、API 请求预算及不重试约束。
- 不运行登录、验证码操作、代理绕行、模型分析、发布或部署。图片下载、实际像素验证、内容哈希去重、权限确认及每日调度由现有采集/素材池负责。

## 当前证据

| 来源 | 获取方式 | 本次验证 | 尚缺 |
| --- | --- | --- | --- |
| Pinterest | 正常登录浏览器可见 Pin 图片 | 将既有 `pinterest-probe-20261105-122903/candidates.json` 的 20 条记录逐条归一化，20 个不同项目，全部用途保持未知；没有再次访问网站 | 服务器正常会话、持续运行与详情图清晰度验收 |
| 站酷 | 公开作品页面或正常授权浏览器；先读取页面自己的 OG 图片 | 合成详情 HTML 测试通过 | 实站可达性、作品选择器、完整项目图组与 CDN 验收 |
| 花瓣 | 正常授权浏览器采集项 | 合成详情 HTML 测试通过 | 登录会话、实站页面结构及无扩展名 CDN 路径验收；未验证路径会拒收 |
| Behance | 公开作品页面或正常授权浏览器；先读取页面自己的 OG 图片 | 合成详情 HTML 测试通过 | 实站访问、完整项目图组及长期稳定性 |
| Pexels 图片 | `https://api.pexels.com/v1/search` 官方照片接口 | 可执行 HTTP client + MockTransport 测试通过 | 可用 API Key 与一次真实照片请求验收 |

五个来源默认 `verified` 都为 `False`。只有显式运营验收配置才改变产品注册状态；不会因适配代码存在自动验收。Pinterest 的 `local_20_pin_sample_only` 区分已有本地样本和生产稳定性，其他为 `fixture_only`。Pexels 视频页不作为图片来源；视频和封面响应不会混入照片解析。

**Pexels 当前限制（2026-11-05）**：[官网 API 页面](https://www.pexels.com/api/)显示新 API Key 签发暂停，暂时无法申请。已有 Key 可以做真实照片请求验收；没有 Key 时保持待配置。不能承诺立即申请成功，也不会改抓网页或视频封面来替代照片 API。

## 调用合同

```python
from omnisignal.workbench.source_adapters import (
    source_catalog, discover_projects, extract_project_metadata,
    normalize_project, normalize_pinterest_sample, fetch_pexels_photos,
)

# 用于页面和计划注册；必须结合运行时凭据/正常浏览器可用性判断是否启用。
metadata = source_catalog()

# 公共 HTML 或普通授权页面快照。失败返回安全错误码，不能降级抓整页图片。
urls = discover_projects("behance", listing_html, limit=80)
article = extract_project_metadata("behance", detail_html, urls[0])

# 已经由正常浏览器明确定位的同一项目图组，不包含导航、推荐流或广告图片。
article = normalize_project("pinterest", {
    "project_url": "https://www.pinterest.com/pin/123/",
    "title": "实际可见标题",
    "description": "实际可见说明",
    "images": [{"url": "https://i.pinimg.com/736x/example.jpg"}],
})

# 直接接现有正常登录原型 records。标记 listing-preview，不冒充原图。
article = normalize_pinterest_sample(record)

# reserve_request 必须是调度器的原子预算扣减函数。False 或异常中止请求。
# Key 从既有私有运行时配置传入，不能加入 URL、日志或报告。
articles = fetch_pexels_photos(
    "packaging", api_key=private_key, reserve_request=reserve_request,
    page=1, per_page=80,
)
```

返回文章包含：`source_id/source_url/native_id/project_id/title/text/author/candidates`；候选图为 `url/alt/declared_width/declared_height/dimension_basis/image_basis`。`project_id` 基于来源及原生 ID，同一 Behance 项目改标题/slug 后仍归一组。下载后的字节哈希仍是最终图片去重依据。

`usage_permissions` 的五项均为 `unknown`，不能用“采集成功”或“AI 判断可用”覆盖授权记录。Pexels API 返回来源也不会自动转成允许发布。这里的权限字段是待接入映射信息，现有素材池仍须保留自己的权限校验。

`declared_width/declared_height` 只是来源声明；必须以下载图像实际解码为准。Pinterest 原型可能选择较大 `srcset` URL 而记录的是当前缩略图尺寸，因此桥接函数不沿用未绑定的尺寸，也不猜测 `/originals/` 地址。

## 边界与调度要求

1. 仅 HTTPS、固定来源页面主机及固定图片 CDN；拒绝用户信息、异常端口、任意第三方地址、SVG/GIF/视频。图片扩展名只是前置过滤，下载仍必须验证 MIME、字节、像素、大小和公共 DNS。
2. 详情默认只取页面自身 `og:image`，不会遍历 `main` 中所有图片。多图需要由受信任采集代码明确定位作品容器，再调用 `normalize_project`；不能把整个推荐流伪装成项目。
3. 请求 Pexels 每页最多 80 张，响应最多 4 MiB；每次请求独立预算，不跟随跳转，不自动重试。401/403、429、网络失败提供不同安全错误码；不记录上游正文。
4. 其他四站的浏览器会话由部署侧提供，模块不会挪用本地个人浏览器 cookie。登录或验证失效应暂停该来源，不能填充虚构成功数。
5. 不足 300 张时汇报可用数量和来源缺口；不得降低去重、内容质量和用途边界来补数。

## 真实采集器集成

五来源已接入 `WorkbenchCollector` 和 `Store.sources()`。这表示工程入口已实现，不表示线上默认启用。

- `OMNISIGNAL_WORKBENCH_VALIDATED_SOURCE_IDS`：逗号分隔、明确完成运营验收的来源 ID。未在列表中的来源无法创建自动采集计划，也无法恢复既有计划；后台运行同样检查，避免绕过页面。
- `OMNISIGNAL_WORKBENCH_PUBLIC_HTML_SOURCE_IDS`：明确确认可用普通公开 HTML 传输的四网页来源。未设置时以“未配置授权浏览器会话/普通网页访问未确认”阻断。没有复制个人 cookie 或自动解决登录验证。此设置必须来自真实访问验收。
- `OMNISIGNAL_PEXELS_API_KEY_FILE`：优先使用的私有凭据文件，必须为绝对路径、普通非符号链接文件、最大 4096 字节。只保存 Key，结尾换行允许；无效文件不回退其他账户凭据。兼容已有 `OMNISIGNAL_PEXELS_API_KEY` 环境变量。不得把值放进命令行、仓库、日志或报告。
- Pexels 计划的 `query` 必须显式提供，最大 200 字。

Pexels 的官方 API 请求及照片下载分别扣除现有持久化每日请求预算。`PhotoPageCursor` 在 SQLite `meta` 中缓存整页归一化结果及页内索引，逐张成功导入后保存索引；只有整页完成才进入下一页。预算耗尽、每日图片目标达到、下载失败和进程重启保留当前位置。每次最多检查三页，空页当天不反复请求，24 小时后从第一页检查新结果。失败图片不会默默跳过，需显示失败并等待后续处理。

新来源保存 `collection_evidence`（来源/图片证据类型、原生项目 ID、来源声明尺寸）；素材实际尺寸仍来自原图解码。`listing-preview` 不会被伪装成 `project-detail`。导入状态为 pending，display/analysis/export 权限均保持 unknown。

已有正常浏览器下载结果可通过 `collection.import_browser_observation(store, source, snapshot, content, image_index=0)` 独立导入。`snapshot` 使用前述归一化输入结构，`content` 为已下载的图片字节。该入口不发网络请求、不启用计划，导入审计记录为 operator，并固定保留 listing-preview；不能把这类手工导入算作定时采集交付。

## 测试

`python -m pytest tests/test_workbench_source_adapters.py -q`：33 passed（2026-11-05）。这是本地工程验证，Pexels 使用模拟 HTTP 响应；其余来源使用人工构造 DOM。另完成既有 Pinterest 20 条真实本地记录的归一化检查，不包含网络重采、模型调用或入库发布。

真实采集器、Store 和请求预算集成测试：使用现有 `D:/coding/OmniSignal/.venv/Scripts/python.exe`，运行 `test_workbench_source_adapters.py`、`test_workbench_five_source_collection.py`、`test_workbench_collection.py`、`test_workbench_acquisition.py` 共 **73 passed**。覆盖四站 fixture 到素材池、Pexels 页内重启、跨日请求预算恢复、每日图片上限、下载失败保留索引、凭据缺失/私有文件无效阻断，以及浏览器预览独立入库。这里网络仍为 fixture；没有宣称线上五站已验收。
