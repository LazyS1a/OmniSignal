# 2026-11-02 DeepFlare 公司共享灵感库

用户确认调整：Omni 自动整理发布，公司在 DeepFlare 统一接收，各品牌按需引用。原来绑定单一品牌的 v1 发布保留，新增独立的 v2 公司库。

## 实现

- v2 manifest、撤回清单与案例用途记录均签署 library recipient，包含 kind、明确的 HTTPS origin 与 library_id。
- 新发布命名空间与旧 v1 分开；旧签名文件、原用途记录、素材原始权限和 revision 均不改写。
- 追加 publication_library_grants、publication_target_deliveries，以资产版本和目标绑定授权依据与交付状态。
- 显式 rebaseline plan 校验后初始化库；自然 worker 重新检查当前 AI 质量、来源、分析许可和用途条件，然后创建新库 full/delta。不重新调用分析模型。
- 全局撤回扇出所有曾交付的库，部分失败报错；已有撤回签名持续阻止重新发布。
- 工作台卡片、详情与自动流程显示当前公司库的发布目标、交付版本和状态。旧品牌已发布不会显示为共享库已交付。
- 新 feed：`/knowledge/v2/libraries/deepflare-company-inspiration`，原 `/knowledge/v1` 保留；沿用既有专用读取身份与签名信任。

## 本地验证

- Python 全量 1096 passed，1 项既有 Starlette 弃用提示。
- 其中共享库新增 21 项，涵盖范围区分、原数据保全、签前关闭、签后恢复、撤回扇出和鉴权。
- 前端 TypeScript、Vite build 与 38 项实际浏览器交互检查通过。浏览器为隔离测试，不充当线上验收。
- B 端使用真实 Python Ed25519 合成签名、记录和图片跨语言验证，通过；未使用线上付费模型。

## 部署与保全

代码提交与线上切换分开记录。计划先完成 B 数据迁移和校验，再切 A 的代码镜像、显式初始化新库和改发布目标。当前记录不宣称已经上线或已被 B 接收。

上线前后保全原图、素材与分析任务、旧 grant、旧发布包和撤回；仅允许追加两张目标库表及新命名空间。辅助验收文件位于 `D:/coding/.novart-tmp/shared-inspiration-20261102/`。
