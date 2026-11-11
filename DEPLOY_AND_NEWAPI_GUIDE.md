# Flow2API 号池部署与新版 NewAPI (rc.47) 接入方案

## 一、它自带 Web 控制台吗？

**有完整的 Web 控制台（支持中英文切换）**，启动后自带三个核心页面：

1. **登录页**：`http://<服务器IP>:38000/login`（默认账号 `admin`，默认密码 `admin`，首次登录请立即修改）。
2. **号池与系统管理后台**：`http://<服务器IP>:38000/manage`
   - **Token（号池）管理**：添加/批量导入 Google Flow 账号 Cookie、查看每个账号剩余积分（如 `20000` Credits）与等级标签（`Ult` / `Pro`）、设置单号图片/视频并发上限、一键刷新积分与登录态、查看错误禁用与 429 冷却状态。
   - **系统配置**：修改对外 API Key、配置请求代理/媒体代理（支持 HTTP / SOCKS5）、开启本地缓存（`/tmp/`）与缓存公网域名、选择打码方式（YesCaptcha / CapSolver / 2Captcha / 容器内置有头浏览器 `personal`）、查看实时请求进度日志。
3. **内置模型在线测试台**：`http://<服务器IP>:38000/test`
   - 无需写代码即可在网页上直接选择图片模型（Nano Banana 2.1 / Lite / Pro）或视频模型（Omni 1.2 Flash / Veo 3.1），上传参考图并流式预览生成结果。

---

## 二、部署方案（推荐 Docker Compose）

### 方案 A：标准轻量部署（搭配 YesCaptcha / CapSolver / 2Captcha 打码，最稳最省内存）

适合 VPS 内存 1–2 GB 即可运行，无需跑 Chromium：

```bash
# 1. 克隆重构后的仓库
git clone https://github.com/momo-api/flow2api.git
cd flow2api

# 2. 复制配置文件
cp config/setting_example.toml config/setting.toml
mkdir -p data tmp

# 3. 构建并后台启动（默认映射宿主机 38000 -> 容器 8000）
docker compose up -d --build

# 4. 查看日志
docker compose logs -f
```

### 方案 B：容器内置有头浏览器打码部署（免第三方打码费）

适合 VPS 内存 ≥ 4 GB，容器内置 `Xvfb + Fluxbox + Chromium`，使用 `personal` 或 `browser` 模式取码：

```bash
git clone https://github.com/momo-api/flow2api.git
cd flow2api
cp config/setting_example.toml config/setting.toml
mkdir -p data tmp

# 启动有头模式（默认映射宿主机 8000 -> 容器 8000）
docker compose -f docker-compose.headed.yml up -d --build
```

---

## 三、把 2 万积分账号放进号池（三步操作）

1. **进入后台先改密码与配置**：
   - 打开 `http://<服务器IP>:38000/manage` 登录：
     - **系统配置 → 管理配置**：修改管理员密码，并设置对外调用的 `API Key`（例如 `sk-flow-pool-xxxx`）。
     - **系统配置 → 代理配置**：如果服务器在国内或非美区原生 IP，开启请求代理和媒体代理（填入美区住宅/原生代理 `http://user:pass@host:port`）。
     - **系统配置 → 缓存配置**：**强烈建议开启「启用缓存」**，若给外部客户端返回 URL，将「缓存访问基础 URL」填为该服务的公网地址（如 `https://flow-pool.yourdomain.com`）。
     - **系统配置 → 验证码配置**：
       - 若用方案 A：选择 `YesCaptcha打码`（推荐 `RecaptchaV3TaskProxylessM1S9`）或 `CapSolver`/`2Captcha`，填入打码 Key。
       - 若用方案 B：选择 `内置浏览器打码 (personal)`。
2. **导出 Google Flow 账号 Cookie 并入池**：
   - 在已登录 `https://flow.google.com` 的浏览器中：
     - **手动导入**：用 Cookie-Editor 导出 JSON 格式或 Header 字符串格式的完整 Google/Flow Cookies，在后台点击 **「新增 Token」** 或 **「批量导入」**，粘贴到 `Google Cookies` 框保存。
     - **插件自动同步（推荐长效保活）**：在维持账号登录的浏览器安装 `Flow2API-Token-Updater` 扩展，填入后台「插件配置」里的连接地址与连接 Token，扩展会自动把刷新的 Cookie 推送到号池。
3. **验证号池状态**：
   - 添加成功后，列表会自动显示邮箱、剩余积分（如 `20000`）和 `Ult`（`PAYGATE_TIER_TWO`）等级标识。
   - 建议将单号并发设为：`图片并发 = 2~4`、`视频并发 = 2`，防止单号瞬时并发过高触发 Google 风控。
   - 也可通过接口随时查看号池汇总：
     ```bash
     curl -H "Authorization: Bearer <你的API_KEY>" http://127.0.0.1:38000/v1/pool/status
     ```

---

## 四、对接新版 NewAPI (`v1.0.0-rc.47`) 完整指南

重构后的 `flow2api` 同时支持新版 NewAPI 的 **Gemini 原生渠道 (Type 24)**、**OpenAI 渠道 (Type 1)** 以及 **异步视频任务插件 (`google` / `sora` / `flow2api`)**。

### 1. 在 NewAPI 中添加 Gemini 渠道（推荐：一条渠道同时跑生图 + Veo 视频）

- **渠道类型**：`Google Gemini`（Type 24）
- **Base URL**：`http://<flow2api内网或公网IP>:38000`（注意不要带 `/v1`）
- **密钥**：填 `flow2api` 后台设置的 `API Key`
- **模型列表**：点击「获取模型列表」或填入：
  - 图片模型：`gemini-3.1-flash-image`、`gemini-3.1-flash-lite-image`、`gemini-3.0-pro-image`、`imagen-3.0-generate-002`
  - 视频模型（自动走 NewAPI `rc.47` 内置 `google` 插件的 `:predictLongRunning` 异步轮询）：`veo-3.1-generate-preview`、`veo-3.1-fast-generate-preview`、`veo-3.0-generate-001`、`veo-3.0-fast-generate-001`

### 2. 在 NewAPI 中添加 OpenAI 渠道（支持 `/v1/images/*`、`/v1/videos`、`/v1/chat/completions`）

- **渠道类型**：`OpenAI`（Type 1）
- **Base URL**：`http://<flow2api内网或公网IP>:38000`
- **密钥**：填 `flow2api` 后台设置的 `API Key`
- **可选导入 Flow 专属任务插件**：
  - 如果希望在 NewAPI 中用 `/v1/videos` 或 `/v1/responses` 直接调用 `omni-1.2-flash`（3–15 秒视频）及 `veo-3.1-lite/fast/quality`，可在 NewAPI 后台导入仓库内的 `deploy/newapi-rc47-flow-plugin.js`。

---

## 五、客户端调用示例

### 1. OpenAI 标准生图（`POST /v1/images/generations`）
```bash
curl -X POST "http://127.0.0.1:38000/v1/images/generations" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image",
    "prompt": "A cyberpunk coffee shop in Tokyo rain, 4k photorealistic",
    "size": "1792x1024",
    "quality": "hd",
    "response_format": "url"
  }'
```

### 2. Gemini 官方生图（`POST /v1beta/models/{model}:generateContent`）
```bash
curl -X POST "http://127.0.0.1:38000/v1beta/models/gemini-3.1-flash-image:generateContent" \
  -H "x-goog-api-key: han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [{"role": "user", "parts": [{"text": "A minimal glass perfume bottle on marble"}]}],
    "generationConfig": {
      "responseModalities": ["IMAGE"],
      "imageConfig": {"aspectRatio": "16:9", "imageSize": "2K"}
    }
  }'
```

### 3. 异步视频生成与轮询（`POST /v1/videos` + `GET /v1/videos/{task_id}`）
```bash
# 1) 提交视频任务（秒回 task_id，不阻塞网关）
curl -X POST "http://127.0.0.1:38000/v1/videos" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "omni-1.2-flash",
    "prompt": "镜头缓缓推进，阳光洒在海面上的帆船",
    "seconds": 8,
    "size": "1280x720"
  }'

# 2) 轮询任务状态
curl -H "Authorization: Bearer han1234" \
  "http://127.0.0.1:38000/v1/videos/<返回的task_id>"

# 3) 直接下载视频流（NewAPI rc.47 自动调用此接口取回 MP4）
curl -L -H "Authorization: Bearer han1234" \
  "http://127.0.0.1:38000/v1/videos/<返回的task_id>/content" -o output.mp4
```
